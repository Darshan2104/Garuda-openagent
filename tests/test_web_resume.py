"""What the session page needs to offer Resume: ownership and the engine's state check, read only."""

from __future__ import annotations

import json
import time
import uuid
from urllib.parse import parse_qs

import pytest

from garuda.core import read_model
from garuda.core.sessions import SessionStore
from garuda.interfaces.web import DashboardConfig, build_context
from garuda.interfaces.web.routes import dispatch
from garuda.interfaces.web.security import TOKEN_HEADER
from garuda.interfaces.web.wire import Request

PORT = 8787
TOKEN = "resume-page-test"


def _get(ctx, path, query=""):
    return dispatch(Request("GET", path, query=parse_qs(query), headers={
        "host": f"127.0.0.1:{PORT}", TOKEN_HEADER: TOKEN}), ctx)


@pytest.fixture(autouse=True)
def linux_supervisor(monkeypatch):
    """The engine's refusals are what these tests check, on any OS: the read model's
    Linux-supervisor test is patched on (the off-Linux answer has its own test)."""
    monkeypatch.setattr(read_model, "owned_supervisor_available", lambda: True)


@pytest.fixture
def store(tmp_path):
    return SessionStore(tmp_path / "sessions")


@pytest.fixture
def ctx(store, tmp_path):
    (tmp_path / "ws").mkdir()
    return build_context(DashboardConfig(port=PORT, token=TOKEN, allow_run=False,
                                         sessions_dir=store.root, workspaces=(tmp_path / "ws",)))


def _flow(store, *, owned=True, state="stopped", stop="flow.interrupted", extra=None):
    sid = str(uuid.uuid4())
    if owned:  # as the engine begins an owned flow: fail-closed published metadata
        store.protect_publication(sid, kind="owned-flow")
    store.begin(sid, task="t", model="m", agent="build", workspace="/tmp", name="flow-" + sid[:4])
    meta = {"kind": "flow", "flow_state": state, "status": "failed" if state != "completed" else "completed",
            "state": {"version": 1, "process": "exited", "work": "done",
                      "outcome": "completed" if state == "completed" else "failed",
                      "verification": {"status": "unavailable"}, "self_check": None},
            "flow": {"name": "two-step", "steps": ["look", "make"]},
            "flow_stop": {"code": stop, "step": "look", "message": f"{stop}: stopped"} if stop else None}
    if owned:
        meta.update(flow_operation={"operation_id": str(uuid.uuid4())})
    meta.update(extra or {})
    store.update_meta(sid, meta)
    return sid


def test_ownership_is_a_read_model_fact_for_rows_and_details(store, ctx):
    owned, cli = _flow(store), _flow(store, owned=False)
    meta = store.load_meta(owned)
    fact = read_model.dashboard_operation(meta)
    assert fact == {"start_operation_id": meta["flow_operation"]["operation_id"],
                    "resume_operation_id": None, "checks": False,
                    "stop": "flow.interrupted", "resumable_stop": True,
                    "dashboard_died": False, "crash": None}
    assert read_model.dashboard_operation(store.load_meta(cli)) is None
    detail = json.loads(_get(ctx, f"/api/sessions/{owned}").body)
    assert detail["dashboard_operation"] == fact
    assert detail["resume_check"] == {"available": True, "code": None, "message": None}
    assert json.loads(_get(ctx, f"/api/sessions/{cli}").body)["resume_check"] is None
    listed = {row["session_id"]: row for row in json.loads(_get(ctx, "/api/sessions").body)["sessions"]}
    assert listed[owned]["dashboard_operation"] == fact and listed[cli]["dashboard_operation"] is None
    recent = {row["session_id"]: row for row in json.loads(_get(ctx, "/api/overview").body)["recent"]}
    assert recent[owned]["dashboard_operation"] == fact


@pytest.mark.parametrize("state,stop,extra,code", [
    ("completed", None, {}, "flow.resume_completed"),
    ("quarantined", "flow.cleanup_unproved", {}, "flow.quarantined"),
    ("stopped", "flow.quota_wait", {"flow_stop": {
        "code": "flow.quota_wait", "step": "look", "message": "wait",
        "cause": {"limit": {"resets_at": int(time.time()) + 3600, "rate_limit_type": "five_hour"}}}},
     "flow.resume_quota_wait"),
])
def test_resume_check_says_what_the_engine_refuses(store, ctx, state, stop, extra, code):
    sid = _flow(store, state=state, stop=stop, extra=extra)
    if stop == "flow.quota_wait":  # the stopped attempt's receipt records the reset
        receipts = store.session_dir(sid) / "flow" / "receipts"
        receipts.mkdir(parents=True)
        (receipts / "look-1.json").write_text(json.dumps({
            "step": "look", "attempt": 1, "index": 0, "status": "stopped",
            "stop": {"code": "flow.quota_wait", "cause": extra["flow_stop"]["cause"]}}))
    check = json.loads(_get(ctx, f"/api/sessions/{sid}").body)["resume_check"]
    assert check["available"] is False and check["code"] == code, check
    if code == "flow.resume_quota_wait":
        assert check["reset_at"] > time.time()


def test_a_resumed_flow_names_its_resume_operation(store):
    resume = str(uuid.uuid4())
    sid = _flow(store, extra={"flow_resume_operation": {"operation_id": resume}})
    assert read_model.dashboard_operation(store.load_meta(sid))["resume_operation_id"] == resume


def test_background_and_role_runs_are_not_dashboard_flows(store):
    for extra in ({"background_operation": {}}, {"launch_kind": "run"}):
        sid = _flow(store, extra=extra)
        assert read_model.dashboard_operation(store.load_meta(sid)) is None


def test_without_the_linux_supervisor_the_check_says_what_the_gateway_says(
        store, ctx, monkeypatch):
    monkeypatch.setattr(read_model, "owned_supervisor_available", lambda: False)
    sid = _flow(store)
    check = json.loads(_get(ctx, f"/api/sessions/{sid}").body)["resume_check"]
    assert check["available"] is False and check["code"] == "starter.ownership_unavailable"
