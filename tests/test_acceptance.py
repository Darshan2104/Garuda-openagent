"""Attributed verification receipts (#158, plan task C.5)."""

import os
import subprocess
import uuid
from pathlib import Path

import pytest

from garuda.core import acceptance as ac
from garuda.core.sessions import SessionStore
from garuda.runtime.session_state import finished, validate
from garuda.types import AgentResult


def _repo(path: Path) -> Path:
    path.mkdir()
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", "init", "-q"], cwd=path, check=True, env=env)
    (path / "a.txt").write_text("a\n")
    subprocess.run(["git", "add", "."], cwd=path, check=True, env=env)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=path, check=True, env=env)
    return path


@pytest.fixture
def ws(tmp_path):
    return _repo(tmp_path / "ws")


def _session(ws, *, success=True, gate=None, changed=(), state=None):
    store = SessionStore()
    sid = str(uuid.uuid4())
    store.begin(sid, task="t", model="m", agent="a", workspace=str(ws))
    result = AgentResult(success=success, final_message="", messages=[], turns=1,
                         metadata={"completion_gate": gate or {}})
    store.finish(sid, result)
    updates = {"delta_changed": list(changed)}
    if state is not None:
        updates["state"] = state
    store.update_meta(sid, updates)
    return store, sid


def _check(run, **extra):
    return {"run": run, **extra}


MATRIX = [
    # name, checks, run ok?, changed, expected verification status, code
    ("trusted pass", [(_check("true"), "user-config")], True, (), "passed", None),
    ("trusted fail", [(_check("false"), "trusted-project")], True, (), "failed", None),
    ("suggestion only", [(_check("true"), "agent-suggested")], True, (), "unavailable",
     "verification.no_trusted_check"),
    ("no check", [], True, (), "unavailable", "verification.no_trusted_check"),
    ("--check", [(_check("true"), "user-request")], True, (), "passed", None),
    ("stale code", [(_check("touch new.txt"), "user-config")], True, (), "invalidated",
     "verification.check_changed_tree"),
    ("relevant infra edit", [(_check("pytest --version"), "user-config")], True,
     ("tests/conftest.py",), "invalidated", "verification.test_infra_changed"),
    ("irrelevant infra edit", [(_check("pytest --version"), "user-config")], True,
     ("package.json",), "passed", None),
    ("runtime failure", [(_check("true"), "user-config")], False, (), "passed", None),
]


@pytest.mark.parametrize("name, checks, ok, changed, status, code", MATRIX,
                         ids=[row[0] for row in MATRIX])
def test_verification_matrix(ws, name, checks, ok, changed, status, code):
    store, sid = _session(ws, success=ok, gate={"verifier": True}, changed=changed)
    before = store.load_meta(sid)

    verification = ac.accept(store, sid, ws, checks)

    after = store.load_meta(sid)
    assert verification["status"] == status
    assert verification.get("code") == code
    state = validate(after["state"])
    # Outcome and self-check never move with verification, and the legacy
    # projection is untouched.
    assert state["outcome"] == before["state"]["outcome"]
    assert state["self_check"] == before["state"]["self_check"]
    assert after["status"] == before["status"] == ("success" if ok else "failed")
    assert len(after["acceptance_receipts"]) == len(checks)
    for receipt in after["acceptance_receipts"]:
        assert receipt["authority"] in ac.ACCEPTANCE + ("agent-suggested",)
        if receipt["status"] in ("passed", "failed", "void", "unverified"):
            assert receipt["fingerprint"]


def test_a_permission_refused_session_keeps_its_outcome(ws):
    refused = {**finished(success=False), "work": "stopped", "outcome": "refused"}
    store, sid = _session(ws, state=refused)
    assert ac.accept(store, sid, ws, [(_check("true"), "user-config")])["status"] == "passed"
    assert store.load_meta(sid)["state"]["outcome"] == "refused"


def test_an_authoritative_grader_verdict_stands_without_checks(ws):
    gate = {"verifier": True, "authoritative_grader": True}
    store, sid = _session(ws, gate=gate)
    assert store.load_meta(sid)["state"]["verification"] == {"status": "passed",
                                                             "authority": "user-config"}
    ac.accept(store, sid, ws, [])
    state = store.load_meta(sid)["state"]
    assert state["verification"]["status"] == "passed"
    assert state["self_check"] == {"status": "passed", "source": "native-completion-gate"}


def test_receipts_refuse_escapes_and_defer_docker(ws):
    store, sid = _session(ws)
    ac.accept(store, sid, ws, [(_check("true", cwd="../.."), "user-config"),
                               (_check("true", mode="docker"), "user-config")])
    statuses = [r["status"] for r in store.load_meta(sid)["acceptance_receipts"]]
    assert statuses == ["refused", "skipped"]


def test_output_is_bounded_and_redacted(ws):
    key = "sk-ant-" + "b" * 40
    store, sid = _session(ws)
    ac.accept(store, sid, ws, [(_check(f"echo {key}; yes x | head -5000"), "user-config")])
    (receipt,) = store.load_meta(sid)["acceptance_receipts"]
    assert key not in receipt["output_tail"] and len(receipt["output_tail"]) <= ac.OUTPUT_TAIL


@pytest.mark.parametrize("run, tool", [
    ("pytest -q", "pytest"), ("python -m pytest -x", "pytest"), ("uv run pytest", "pytest"),
    ("npm test", "npm"), (["cargo", "test"], "cargo"), ("CI=1 make test", "make"),
])
def test_infra_patterns_follow_the_tool(run, tool):
    assert ac.infra_patterns(run) == ac.INFRA[tool]


def test_a_check_flag_verifies_an_acp_session(tmp_path, monkeypatch, capsys):
    from tests.test_runtime_cli import _install_shim, _main, _on_path, _trusted_settings

    ws = _repo(tmp_path / "w")
    _on_path(monkeypatch, tmp_path / "bin")
    _install_shim(tmp_path / "bin", profile="success")
    _trusted_settings(tmp_path, monkeypatch)

    code, out = _main(monkeypatch, capsys, "run", "-t", "hi", "--workspace", str(ws),
                      "--runtime", "fakeacp", "--check", "test -f a.txt")

    assert code == 0, out
    assert "[garuda] verification: passed (user-request)" in out
    (meta,) = SessionStore().list_sessions()
    assert meta["state"]["verification"] == {"status": "passed", "authority": "user-request"}
    assert meta["status"] == "completed"
