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


def _seed(store, session_id="s1"):
    store.begin(session_id, task="move it", model="m", agent="a", workspace="w")
    store.ensure_unified(session_id)
    return session_id


def test_runtimes_picker_lists_health(ctx):
    records = payload(call(ctx, "/api/runtimes"))
    assert {r["runtime_id"] for r in records} >= {"native", "claude", "codex"}
    by_id = {r["runtime_id"]: r for r in records}
    assert by_id["native"]["available"] is True
    assert by_id["claude"]["quota"] is None
    assert by_id["claude"]["login"]["flow"] == "user-cli"


def test_runtime_inspect_shows_auth_guidance(ctx):
    record = payload(call(ctx, "/api/runtimes/claude"))
    assert record["runtime_id"] == "claude"
    assert any("vendor's policy" in line for line in record["auth_guidance"])
    missing = call(ctx, "/api/runtimes/nope")
    assert missing.status == 404


def test_handoff_preview_is_read_only_but_prepare_writes(ctx, ro_ctx, store):
    _seed(store)
    preview = payload(call(ro_ctx, "/api/runs/s1/handoff", query="to=codex"))
    assert preview["target_runtime"] == "codex"
    assert preview["requires_confirm"] is True
    assert store.load_unified("s1").handoff["state"] == "none"

    refused = call(ro_ctx, "/api/runs/s1/handoff", method="POST", body={"target": "codex"})
    assert refused.status == 503
    assert store.load_unified("s1").handoff["state"] == "none"

    prepared = payload(call(ctx, "/api/runs/s1/handoff", method="POST", body={"target": "codex"}))
    assert prepared["handoff_state"] == "prepared"
    assert store.load_unified("s1").handoff["state"] == "prepared"

    bad = call(ctx, "/api/runs/s1/handoff", method="POST", body={})
    assert bad.status == 400
    missing_to = call(ctx, "/api/runs/s1/handoff")
    assert missing_to.status == 400


def test_diff_timeline_and_recover(ctx, ro_ctx, store):
    _seed(store)
    timeline = payload(call(ctx, "/api/runs/s1/diff"))
    assert timeline["session_id"] == "s1"
    assert isinstance(timeline["files"], list)

    classification = payload(call(ro_ctx, "/api/runs/s1/recover"))
    assert classification["state"] == "resumable"
    report = payload(call(ctx, "/api/runs/s1/recover", method="POST"))
    assert report["resume_session_id"] == "s1"

    refused = call(ro_ctx, "/api/runs/s1/recover", method="POST")
    assert refused.status == 503
    missing = call(ctx, "/api/runs/nope/diff")
    assert missing.status == 404
