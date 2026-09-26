"""Dashboard runtime-control tests for issue #38 (P1.7).

Picker, health, capability display, approval-adjacent auth guidance, handoff
preview vs prepare, diff timeline, and failed-switch recovery — through the
pure dispatch table, so no socket and no browser. Security posture is
unchanged: reads stay reads, prepares require write mode.
"""

import json

import pytest

from garuda.core.sessions import SessionStore
from garuda.interfaces.web.routes import DashboardContext, dispatch
from garuda.interfaces.web.security import TOKEN_HEADER
from garuda.interfaces.web.wire import Request

PORT = 8787
TOKEN = "test-token-value"


@pytest.fixture
def store(tmp_path):
    return SessionStore(root=tmp_path / "sessions")


@pytest.fixture
def ctx(store):
    return DashboardContext(port=PORT, token=TOKEN, store=store, allow_run=True)


@pytest.fixture
def ro_ctx(store):
    return DashboardContext(port=PORT, token=TOKEN, store=store, allow_run=False)


def call(ctx, path, method="GET", query="", body=None):
    from urllib.parse import parse_qs

    headers = {"host": f"127.0.0.1:{PORT}", TOKEN_HEADER: TOKEN}
    if method != "GET":
        headers["origin"] = f"http://127.0.0.1:{PORT}"
    request = Request(
        method=method,
        path=path,
        query=parse_qs(query),
        headers=headers,
        body=json.dumps(body).encode() if body is not None else b"",
    )
    return dispatch(request, ctx)


def payload(response):
    return json.loads(response.body)


def _seed(store, session_id="s1", workspace="."):
    store.begin(session_id, task="move it", model="m", agent="a", workspace=workspace)
    store.checkpoint_messages(session_id, [])
    store.ensure_unified(session_id)
    return session_id


def test_runtimes_picker_lists_health(ctx):
    records = payload(call(ctx, "/api/runtimes"))
    assert {r["runtime_id"] for r in records} >= {"native", "claude", "codex"}
    by_id = {r["runtime_id"]: r for r in records}
    assert by_id["native"]["available"] is True
    assert by_id["claude"]["quota"] is None
    assert by_id["claude"]["login"]["flow"] == "user-cli"


def test_runtimes_use_trusted_global_registry(ctx, tmp_path, monkeypatch):
    settings = tmp_path / "settings.yaml"
    settings.write_text(
        "runtimes:\n"
        "  - runtime_id: offline\n"
        "    kind: acp\n"
        "    command: [not-installed-garuda-runtime]\n"
        "    version: '1'\n"
        "    setup: Install offline.\n"
        "  - runtime_id: disabled\n"
        "    kind: acp\n"
        "    command: [not-installed-garuda-disabled]\n"
        "    version: '1'\n"
        "disabled_runtimes: [disabled]\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("GARUDA_GLOBAL_SETTINGS", str(settings))
    ctx.workspace = tmp_path

    records = payload(call(ctx, "/api/runtimes"))
    by_id = {record["runtime_id"]: record for record in records}
    assert by_id["offline"]["available"] is False
    assert by_id["disabled"]["available"] is False
    assert any("disabled by user configuration" in warning for warning in by_id["disabled"]["warnings"])

    _seed(ctx.store)
    for target in ("offline", "disabled"):
        refused = call(ctx, "/api/runs/s1/handoff", method="POST", body={"target": target})
        assert refused.status == 404
    assert ctx.store.load_unified("s1").handoff["state"] == "none"


def test_runtime_inspect_shows_auth_guidance(ctx):
    record = payload(call(ctx, "/api/runtimes/claude"))
    assert record["runtime_id"] == "claude"
    assert any("vendor's policy" in line for line in record["auth_guidance"])
    missing = call(ctx, "/api/runtimes/nope")
    assert missing.status == 404


def test_handoff_preview_is_read_only_but_prepare_writes(ctx, ro_ctx, store):
    _seed(store)
    preview = payload(call(ro_ctx, "/api/runs/s1/handoff", query="to=native"))
    assert preview["target_runtime"] == "native"
    assert preview["requires_confirm"] is True
    assert store.load_unified("s1").handoff["state"] == "none"

    refused = call(ro_ctx, "/api/runs/s1/handoff", method="POST", body={"target": "native"})
    assert refused.status == 503
    assert store.load_unified("s1").handoff["state"] == "none"

    prepared = payload(call(ctx, "/api/runs/s1/handoff", method="POST", body={"target": "native"}))
    assert prepared["handoff_state"] == "prepared"
    assert store.load_unified("s1").handoff["state"] == "prepared"

    unknown = call(ro_ctx, "/api/runs/s1/handoff", query="to=missing-runtime")
    assert unknown.status == 404


def test_handoff_rejects_unavailable_custom_target(ctx, store):
    _seed(store)
    ctx.extra = {
        "manifests": [
            {
                "runtime_id": "offline",
                "kind": "acp",
                "command": ["not-installed-garuda-runtime"],
                "version": "1",
                "setup": "Install offline.",
            }
        ]
    }
    refused = call(ctx, "/api/runs/s1/handoff", method="POST", body={"target": "offline"})
    assert refused.status == 404
    assert store.load_unified("s1").handoff["state"] == "none"

    bad = call(ctx, "/api/runs/s1/handoff", method="POST", body={})
    assert bad.status == 400
    missing_to = call(ctx, "/api/runs/s1/handoff")
    assert missing_to.status == 400


def test_diff_timeline_and_recover(ctx, ro_ctx, store):
    _seed(store)
    timeline = payload(call(ctx, "/api/runs/s1/diff"))
    assert timeline["session_id"] == "s1"
    # No recorded baseline: explicit, never invented.
    assert timeline["baseline_recorded"] is False
    assert timeline["files"] == []

    from garuda.workspace.diff import capture_baseline

    store.record_baseline("s1", capture_baseline(".").to_dict())
    timeline = payload(call(ctx, "/api/runs/s1/diff"))
    assert timeline["baseline_recorded"] is True
    assert isinstance(timeline["files"], list)

    _seed(store, "missing-workspace", workspace="does-not-exist")
    store.record_baseline("missing-workspace", capture_baseline(".").to_dict())
    unavailable = call(ctx, "/api/runs/missing-workspace/diff")
    assert unavailable.status == 404

    classification = payload(call(ro_ctx, "/api/runs/s1/recover"))
    assert classification["state"] == "resumable"
    report = payload(call(ctx, "/api/runs/s1/recover", method="POST"))
    assert report["resume_session_id"] == "s1"

    refused = call(ro_ctx, "/api/runs/s1/recover", method="POST")
    assert refused.status == 503
    missing = call(ctx, "/api/runs/nope/diff")
    assert missing.status == 404
