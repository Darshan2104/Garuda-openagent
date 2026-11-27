"""The Flows page reads: every available flow, pure, isolated per flow, transport-guarded."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
from pathlib import Path
from urllib.parse import parse_qs

import pytest

from garuda.config import garuda_yaml as gy
from garuda.core.sessions import SessionStore
from garuda.interfaces.web import DashboardConfig, build_context
from garuda.interfaces.web.routes import dispatch
from garuda.interfaces.web.security import TOKEN_HEADER
from garuda.interfaces.web.wire import Request

PORT = 8787
TOKEN = "flows-page-test"
ROLES = ("scout", "planner", "coder", "reviewer")
MINE = {"description": "Two steps of mine", "steps": [
    {"id": "look", "role": "scout", "write_policy": "no-edits", "outputs": ["notes"]},
    {"id": "make", "role": "coder", "inputs": [{"type": "notes", "from": "look"}],
     "outputs": ["plan"]}]}
FLOWS = {
    "mine": MINE,
    "reviewed": {"steps": [
        {"id": "build", "role": "coder", "outputs": ["patch"],
         "review": {"by": "reviewer", "max_rounds": 2}},
        {"id": "check", "role": "reviewer", "write_policy": "no-edits", "inputs": ["patch"],
         "outputs": ["review"]}]},
    "fanout": {"steps": [
        {"id": "look", "parallel": ["scout", "reviewer"], "outputs": ["notes"]},
        {"id": "make", "role": "coder", "inputs": ["notes"], "outputs": ["plan"]}]},
    # Valid configuration whose role cannot be resolved: its agent does not exist.
    "ghostly": {"steps": [{"id": "haunt", "role": "haunter", "outputs": ["plan"]}]},
}


def _roles():
    roles = {role: {"harness": "native", "model_id": f"{role}/model"} for role in ROLES}
    roles["haunter"] = {"harness": "native", "agent": "missing-agent"}
    return roles


def _config(**extra):
    doc = {"version": 1, "roles": _roles(), "flows": FLOWS}
    doc.update(extra)
    gy.user_path().write_text(gy.dump(doc))


def _get(ctx, path, *, query="", headers=None):
    request_headers = {"host": f"127.0.0.1:{PORT}", TOKEN_HEADER: TOKEN}
    request_headers.update(headers or {})
    return dispatch(Request("GET", path, query=parse_qs(query), headers=request_headers), ctx)


def _body(response):
    return json.loads(response.body)


def _snapshot(*roots):
    return {(str(root), str(p.relative_to(root))): p.read_bytes() if p.is_file() else None
            for root in roots if root.exists() for p in root.rglob("*")}


def _forbid_execution(monkeypatch):
    from garuda.agents.setup import RuntimeCatalog

    def forbidden(*_args, **_kwargs):
        raise AssertionError("a Flows read started execution, discovery or a probe")

    for target, name in ((subprocess, "run"), (subprocess, "Popen"), (socket, "create_connection"),
                         (socket.socket, "connect"), (asyncio, "create_subprocess_exec"),
                         (RuntimeCatalog, "discover")):
        monkeypatch.setattr(target, name, forbidden)
    from garuda.scenarios.service import StarterService

    monkeypatch.setattr(StarterService, "start", forbidden)


@pytest.fixture
def context(tmp_path):
    workspaces = (tmp_path / "first", tmp_path / "second")
    for ws in workspaces:
        ws.mkdir()
    _config()
    return build_context(DashboardConfig(port=PORT, token=TOKEN, allow_run=False,
                                         sessions_dir=SessionStore().root, workspaces=workspaces))


def _roots(ctx):
    return (*ctx.allowed_workspaces, ctx.store.root, gy.user_path().parent,
            Path(os.environ["GARUDA_LEASES_DIR"]))


def test_library_lists_every_flow_purely_with_pipelines_and_reasons(context, monkeypatch):
    _forbid_execution(monkeypatch)
    before = _snapshot(*_roots(context))
    response = _get(context, "/api/flows")
    assert response.status == 200, response.body
    data = _body(response)
    assert _snapshot(*_roots(context)) == before
    rows = {row["name"]: row for row in data["flows"]}
    assert set(rows) >= {"mine", "reviewed", "fanout", "ghostly", "plan-only", "pair",
                         "plan-build-review"}
    assert data["host"]["mode"] == "read-only" and data["host"]["start_available"] is False
    assert data["config_error"] is None
    mine = rows["mine"]
    assert (mine["source"], mine["source_file"], mine["trusted"]) == (
        "user", str(gy.user_path()), None)
    assert [s["id"] for s in mine["pipeline"]["steps"]] == ["look", "make"]
    assert mine["pipeline"]["steps"][1]["inputs"] == [
        {"type": "notes", "from": "look", "producer": "look"}]
    assert mine["readiness"]["status"] in {"ready", "needs-setup", "not-checked"}
    assert mine["start"]["supported"] is False
    assert [r["code"] for r in mine["start"]["reasons"]] == ["dashboard.read_only"]
    assert rows["plan-only"]["source"] == "packaged" and rows["plan-only"]["source_file"] is None
    review = rows["reviewed"]["pipeline"]["steps"][0]["review"]
    assert review == {"by": "reviewer", "max_rounds": 2, "independent": True}
    # A shape the owned engine refuses is named even in read-only mode.
    assert "starter.parallel_ownership_unavailable" in {
        r["code"] for r in rows["fanout"]["start"]["reasons"]}
    assert rows["fanout"]["pipeline"]["steps"][0]["kind"] == "parallel"
    # One flow whose role cannot be resolved is listed with its error and a static pipeline.
    ghost = rows["ghostly"]
    assert ghost["error"]["code"].startswith("agent."), ghost["error"]
    assert ghost["pipeline"]["resolution"] == "static"
    assert [s["id"] for s in ghost["pipeline"]["steps"]] == ["haunt"]
    assert ghost["readiness"] is None and ghost["start"]["supported"] is False
    assert ghost["error"]["code"] in {r["code"] for r in ghost["start"]["reasons"]}
    assert isinstance(data["logins"], dict)


def test_detail_answers_one_flow_and_unknown_names_are_404(context, monkeypatch):
    _forbid_execution(monkeypatch)
    response = _get(context, "/api/flows/mine", query="workspace=1")
    assert response.status == 200
    data = _body(response)
    assert data["flow"]["name"] == "mine"
    assert data["workspace"] == str(context.allowed_workspaces[1])
    library = {row["name"]: row for row in _body(_get(context, "/api/flows", query="workspace=1"))["flows"]}
    assert data["flow"] == library["mine"]
    missing = _get(context, "/api/flows/nothing-here")
    assert missing.status == 404 and _body(missing)["error"]["code"] == "not_found"
    assert _get(context, "/api/flows/Bad%20Name").status == 404


@pytest.mark.parametrize("query", [
    "workspace=2", "workspace=-1", "workspace=x", "workspace=0&workspace=1", "extra=1",
])
def test_queries_are_validated(context, query):
    for path in ("/api/flows", "/api/flows/mine"):
        response = _get(context, path, query=query)
        assert response.status == 400, (path, query)


@pytest.mark.parametrize("headers,status", [
    ({TOKEN_HEADER: "wrong"}, 401), ({"host": "rebound.test:8787"}, 403),
    ({"origin": "http://elsewhere.test"}, 403), ({TOKEN_HEADER: ""}, 401),
])
def test_flow_reads_use_existing_transport_security(context, headers, status):
    for path in ("/api/flows", "/api/flows/mine"):
        assert _get(context, path, headers=headers).status == status


def test_a_packaged_flow_with_missing_roles_names_them(context):
    roles = _roles()
    del roles["planner"]
    gy.user_path().write_text(gy.dump({"version": 1, "roles": roles, "flows": {"mine": MINE}}))
    row = _body(_get(context, "/api/flows/plan-only"))["flow"]
    assert row["error"]["code"] == "starter.missing_roles"
    assert row["missing_roles"] == ["planner"] and "planner" in row["error"]["fix"]
    assert row["pipeline"]["resolution"] == "static"
    assert row["pipeline"]["missing_roles"] == ["planner"]


def test_a_broken_configuration_still_lists_the_packaged_flows(context):
    gy.user_path().write_text("version: 1\nflows:\n  bad:\n    steps: []\n")
    data = _body(_get(context, "/api/flows"))
    assert data["config_error"]["code"].startswith("config.")
    assert {row["name"] for row in data["flows"]} == {"plan-only", "pair", "plan-build-review"}
    assert all(row["error"] for row in data["flows"])  # no roles without a configuration


def test_a_project_flow_is_listed_with_its_trust_state(context):
    ws = context.allowed_workspaces[0]
    (ws / "garuda.yaml").write_text(gy.dump({"version": 1, "flows": {"team": {
        "steps": [{"id": "only", "role": "coder", "outputs": ["plan"]}]}}}))
    row = _body(_get(context, "/api/flows/team"))["flow"]
    assert (row["source"], row["source_file"], row["trusted"]) == (
        "project", str(ws / "garuda.yaml"), False)
    assert "flow.project_source" in {d["code"] for d in row["readiness"]["diagnostics"]}
    assert row["pipeline"]["trusted"] is False


def test_library_pipeline_equals_the_preview_pipeline(context):
    library = _body(_get(context, "/api/flows/mine"))["flow"]
    request_headers = {"host": f"127.0.0.1:{PORT}", TOKEN_HEADER: TOKEN,
                       "origin": f"http://127.0.0.1:{PORT}"}
    response = dispatch(Request("POST", "/api/scenarios/preview", headers=request_headers,
                                body=json.dumps({"starter_id": "run-flow", "inputs": {
                                    "flow": "mine", "goal": "go"}}).encode()), context)
    assert response.status == 200, response.body
    preview = _body(response)
    assert preview["pipeline"] == library["pipeline"]
    assert preview["readiness"] == library["readiness"]


@pytest.fixture
async def write_context(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _config()
    monkeypatch.setattr(sys, "platform", "linux")
    ctx = build_context(DashboardConfig(
        port=PORT, token=TOKEN, allow_run=True, sessions_dir=tmp_path / "sessions",
        workspaces=(workspace,), open_browser=False), loop=asyncio.get_running_loop())
    try:
        yield ctx
    finally:
        await ctx.flow_starts.aclose()
        await ctx.live.aclose()


async def test_write_mode_reports_owned_start_support_per_flow(write_context, monkeypatch):
    ctx = write_context
    roles = _roles()
    roles["reviewer"]["permissions"] = "yolo"
    _config(roles=roles)
    data = _body(await asyncio.to_thread(_get, ctx, "/api/flows"))
    rows = {row["name"]: row for row in data["flows"]}
    assert data["host"] == {"mode": "read-write", "start_available": True, "platform": "linux",
                            "max_permission": "smart"}
    assert rows["mine"]["start"] == {"supported": True, "reasons": []}
    assert "starter.parallel_ownership_unavailable" in {
        r["code"] for r in rows["fanout"]["start"]["reasons"]}
    assert "starter.permission_exceeds_ceiling" in {
        r["code"] for r in rows["reviewed"]["start"]["reasons"]}
    assert not (ctx.store.root / "launch-intents").exists()
    assert ctx.store.list_sessions() == []


async def test_non_linux_write_mode_is_copy_only(write_context, monkeypatch):
    ctx = write_context
    ctx.flow_starts.backend_available = False
    row = _body(await asyncio.to_thread(_get, ctx, "/api/flows/mine"))["flow"]
    assert row["start"]["supported"] is False
    assert "starter.ownership_unavailable" in {r["code"] for r in row["start"]["reasons"]}


# -- approvals of dashboard-started flows, visible beside the session-file ones -----------


def _post(ctx, path, payload):
    headers = {"host": f"127.0.0.1:{PORT}", TOKEN_HEADER: TOKEN, "origin": f"http://127.0.0.1:{PORT}"}
    return dispatch(Request("POST", path, headers=headers, body=json.dumps(payload).encode()), ctx)


async def test_inbox_and_overview_list_the_broker_asks_of_dashboard_operations(write_context, monkeypatch):
    import uuid

    ctx = write_context
    workspace = str(ctx.allowed_workspaces[0])
    operation, outside = str(uuid.uuid4()), str(uuid.uuid4())
    session = str(uuid.uuid4())
    records = {operation: {"workspace": workspace, "session_id": session},
               outside: {"workspace": "/elsewhere", "session_id": str(uuid.uuid4())}}
    monkeypatch.setattr(ctx.flow_starts.intents, "read", lambda owner: records.get(owner))
    broker = ctx.live.approvals
    tasks = [asyncio.create_task(broker.make_handler(owner, lambda: None)(f"bash(echo {owner[:4]})"))
             for owner in (operation, outside, uuid.uuid4().hex)]  # the last: a chat's ask
    await asyncio.sleep(0)
    seen = dict(broker._seen)

    inbox = _body(await asyncio.to_thread(_get, ctx, "/api/inbox"))
    assert inbox["approvals"] == []  # the shared read model's list is untouched
    (live,) = inbox["live"]
    assert (live["owner"], live["operation_id"], live["session_id"], live["source"]) == (
        operation, operation, session, "dashboard-operation")
    assert live["grace_seconds"] == broker.grace_seconds and live["digest"]
    overview = _body(await asyncio.to_thread(_get, ctx, "/api/overview"))
    assert overview["approvals"]["live"] == 1 and overview["approvals"]["waiting"] == 1
    assert dict(broker._seen) == seen  # listing refreshes no heartbeat

    stale = await asyncio.to_thread(_post, ctx, f"/api/approvals/{live['ask_id']}",
                                    {"approved": True, "owner": live["owner"], "digest": "0" * 64})
    assert stale.status == 409 and _body(stale)["error"]["code"] == "approval_mismatch"
    answered = await asyncio.to_thread(_post, ctx, f"/api/approvals/{live['ask_id']}",
                                       {"approved": True, "owner": live["owner"], "digest": live["digest"]})
    assert answered.status == 200, answered.body
    assert await asyncio.wait_for(tasks[0], 5) is True
    assert broker.settled(operation)[0]["decided_by"] == "you"
    assert _body(await asyncio.to_thread(_get, ctx, "/api/inbox"))["live"] == []
    for task in tasks[1:]:
        task.cancel()
    await asyncio.gather(*tasks[1:], return_exceptions=True)


async def test_a_steps_own_file_request_for_a_live_ask_is_counted_once(write_context, monkeypatch):
    """An owned step also publishes its ask in its session's file channel: one approval."""
    import time
    import uuid

    from garuda.acp.approval_channel import FileApprovalChannel
    from garuda.acp.broker import ApprovalRequest
    from garuda.runtime import session_state

    ctx = write_context
    operation, flow_session, step = str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())
    record = {"workspace": str(ctx.allowed_workspaces[0]), "session_id": flow_session}
    monkeypatch.setattr(ctx.flow_starts.intents, "read", lambda owner: record if owner == operation else None)
    action = "bash({'command': 'rm -rf build/'})"
    store = ctx.store
    store.begin(step, task="t", model="m", agent="build", workspace=record["workspace"])
    store.update_meta(step, {"state": {**session_state.started(), "work": "waiting"},
                             "flow_step": {"flow_session": flow_session, "step": "plan", "attempt": 1}})
    FileApprovalChannel(store.session_dir(step) / "approvals", step).publish(
        ApprovalRequest("apr-1", action, "terminal", "native", step),
        ceiling="smart", expires_at=time.time() + 3600)
    task = asyncio.create_task(ctx.live.approvals.make_handler(operation, lambda: None)(action))
    await asyncio.sleep(0)

    inbox = _body(await asyncio.to_thread(_get, ctx, "/api/inbox"))
    assert [row["approval_id"] for row in inbox["approvals"]] == ["apr-1"]  # read model as is
    (live,) = inbox["live"]
    assert live["covers"] == [{"session_id": step, "approval_id": "apr-1"}]
    overview = _body(await asyncio.to_thread(_get, ctx, "/api/overview"))["approvals"]
    assert (overview["waiting"], overview["live"], overview["sessions"]) == (1, 1, 1)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


def test_a_read_only_inbox_lists_no_dashboard_asks(context):
    assert _body(_get(context, "/api/inbox"))["live"] == []
