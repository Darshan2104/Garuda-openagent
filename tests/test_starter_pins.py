"""The five packaged starters' previews, digests, commands and refusals are pinned.

Each fingerprint below was computed on origin/main before the ``run-flow``
starter existed (commit 184702ee). A generic starter must not move what the five
existing starters preview, digest, show as their equivalent command or refuse.
Machine-specific values (temporary paths, inode identity, the server epoch)
are replaced by placeholders, and every digest is recomputed from its
canonical input, so a pin is equivalent to pinning the digest itself.
If a pin must change, the change in that starter's behaviour needs a review.
"""

import hashlib
import json
import os
import sys

import pytest
import yaml

from garuda.config import garuda_yaml as gy
from garuda.core.sessions import SessionStore
from garuda.scenarios.compile import compile_with_context
from garuda.scenarios.digests import digest
from garuda.scenarios.gateway import FlowStartGateway
from garuda.scenarios.service import RESOLUTION_ERRORS, StarterService

STARTERS = {
    "plan-change": {"goal": "Add status", "constraints": "Keep retries"},
    "plan-feedback": {"feedback": "Status is unclear", "desired": "Explain it"},
    "build-review": {"goal": "Add status", "requirements": "Deterministic tests"},
    "run-with-role": {"goal": "Add status", "options": {"name": "pin", "checks": ["pytest -q"]}},
    "ask-role": {"question": "Can status reuse the retry client?"},
}
REFUSALS = [
    ("plan-change", {"goal": "x", "options": {"checks": ["true"]}}),
    ("plan-change", {}),
    ("plan-change", {"goal": "x", "variant": "pair"}),
    ("plan-change", {"goal": "x", "flow": "pair"}),
    ("build-review", {"goal": "x", "variant": "pair"}),
    ("build-review", {"goal": "x", "variant": "elsewhere"}),
    ("run-with-role", {"goal": "x", "role": "missing"}),
    ("run-with-role", {"goal": "x", "role": "Bad Role"}),
    ("ask-role", {"question": "x", "options": {"checks": ["true"]}}),
    ("unknown-starter", {"goal": "x"}),
]
PINS = {
    "default": {
        "ask-role": "ec4ed80dd4306c8d8064b475826b3d3192c263a9a4c3a41d71b236443195219b",
        "build-review": "8900cc92d51114493d2aca1856ba5e33fcaa6529ef00c6e47bccaaad1041feb5",
        "plan-change": "bd8c0d4321b29e35b44a3fa2932fdb9c000d8642e5eaa40b3f656af3bb91ad64",
        "plan-feedback": "9560ec057fdc4dda5b5e4918fb49f3144454a2fcc73fda86c2f13fc4604479bf",
        "refusals": "299630589b6938fd618ec74c8ba70e8e2b156acb96fc706180223486b150be5e",
        "run-with-role": "eeb2f12885657e71c3b66a5415b02a39b664ba0bed50cdb5c857d2fc3cd07511",
    },
    "checked": {
        "ask-role": "c3d9ad3e28becf8ef9309327dc078a08b5866db52c417ac3dd6eeafe88d2e8cf",
        "build-review": "339885a0f3bda708a4ac17c40cb2f7ea69ca432516be561276ef8a0344285cc3",
        "plan-change": "6f22f7e823fe5a223ab4b6b24e2ffb373ebd9f1af6aa6e867881438519a1d65a",
        "plan-feedback": "a57aa04f407e3186e28ade5a096e513bca56ea3a073a88965a1a4539d1b9146c",
        "refusals": "299630589b6938fd618ec74c8ba70e8e2b156acb96fc706180223486b150be5e",
        "run-with-role": "4db27b12a943f77054039421db8f03106d3d3947d03eb240df48a15ee8a2710b",
    },
}


class _Approvals:
    def make_handler(self, *_args):  # pragma: no cover - never started here
        raise AssertionError("pins never start a flow")


@pytest.fixture(params=["default", "checked"])
def pinned(request, tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    ws = tmp_path / "workspace"
    ws.mkdir()
    config = {"version": 1, "roles": {
        role: {"harness": "native", "model_id": role + "/model"}
        for role in ("scout", "planner", "coder", "reviewer")}}
    if request.param == "checked":
        config["checks"] = [{"run": "python -m pytest -q"}]
    gy.user_path().write_text(yaml.safe_dump(config))
    return request.param, ws


def _normal(value, ws, identity):
    text = json.dumps(value, sort_keys=True, ensure_ascii=False)
    replacements = {str(ws): "<workspace>", str(gy.user_path().parent): "<settings>",
                    os.environ["GARUDA_SESSIONS_DIR"]: "<sessions>",
                    os.environ["GARUDA_LEASES_DIR"]: "<leases>"}
    for old, new in replacements.items():
        text = text.replace(old, new)
    if identity is not None:
        text = text.replace(json.dumps(identity), '"<identity>"')
    return json.loads(text)


def _fingerprint(starter, ws):
    store = SessionStore()
    inputs = STARTERS[starter]
    plan, _context = compile_with_context(starter, inputs, ws, store=store)
    identity = plan.provenance["workspace_identity"]
    values = {key: value for key, value in plan.to_dict().items() if key != "digest"}
    assert plan.digest == digest(values)
    gateway = FlowStartGateway(store=store, workspaces=(ws,), max_permission="smart",
                               approvals=_Approvals())
    gateway.backend_available = True
    gateway.server_epoch = "0" * 32
    owned = gateway.preview(starter, inputs, ws)
    assert owned["authority"]["digest"] == digest(owned["authority"]["envelope"])
    cli = StarterService(store).preview(starter, inputs, ws)
    for preview in (owned, cli):
        assert preview["plan"]["digest"] == plan.digest
        preview["plan"].pop("digest")
    owned["authority"].pop("digest")
    library = next(row for row in StarterService(store).list(ws) if row["id"] == starter)
    document = {"plan": values, "owned": owned, "cli": cli, "library": library}
    return hashlib.sha256(json.dumps(_normal(document, ws, identity), sort_keys=True,
                                     ensure_ascii=False).encode()).hexdigest()


def _refusals(ws):
    out = []
    for starter, inputs in REFUSALS:
        try:
            compile_with_context(starter, inputs, ws, store=SessionStore())
        except RESOLUTION_ERRORS as exc:
            out.append([starter, getattr(exc, "code", None), str(exc)])
        else:
            out.append([starter, "accepted", None])
    return out


def test_existing_starter_fingerprints_are_unchanged(pinned):
    name, ws = pinned
    computed = {starter: _fingerprint(starter, ws) for starter in STARTERS}
    computed["refusals"] = hashlib.sha256(json.dumps(
        _normal(_refusals(ws), ws, None)).encode()).hexdigest()
    assert computed == PINS.get(name)


def test_library_lists_exactly_the_five_existing_starters(pinned):
    _name, ws = pinned
    assert [row["id"] for row in StarterService(SessionStore()).list(ws)] == [
        "ask-role", "build-review", "plan-change", "plan-feedback", "run-with-role"]
