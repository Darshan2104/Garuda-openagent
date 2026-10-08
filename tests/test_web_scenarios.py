"""Read-only starter HTTP transport: authorization, CLI parity, and no launch."""

import asyncio
import json
import os
import socket
import subprocess
from urllib.parse import parse_qs

import pytest

from garuda.config import garuda_yaml as gy
from garuda.core.sessions import SessionStore
from garuda.interfaces import scenario_cli
from garuda.interfaces.main import build_parser
from garuda.interfaces.web import DashboardConfig, build_context
from garuda.interfaces.web.routes import dispatch
from garuda.interfaces.web.security import TOKEN_HEADER
from garuda.interfaces.web.wire import Request
from tests.test_scenarios import completed_plan as completed_plan

PORT = 8787
TOKEN = "starter-browser-test"


def _call(ctx, path, *, payload=None, query="", headers=None):
    method = "POST" if payload is not None else "GET"
    request_headers = {"host": f"127.0.0.1:{PORT}", TOKEN_HEADER: TOKEN}
    if method == "POST":
        request_headers["origin"] = f"http://127.0.0.1:{PORT}"
    request_headers.update(headers or {})
    return dispatch(Request(method, path, query=parse_qs(query), headers=request_headers,
                            body=json.dumps(payload).encode() if payload is not None else b""), ctx)


def _body(response):
    return json.loads(response.body)


def _snapshot(*roots):
    return {(str(root), str(p.relative_to(root))): p.read_bytes() if p.is_file() else None
            for root in roots for p in root.rglob("*")}


def _forbid_execution(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("read-only HTTP starter operation started execution")

    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden)
    from garuda.scenarios.service import StarterService

    monkeypatch.setattr(StarterService, "start", forbidden)


@pytest.fixture
def context(tmp_path):
    workspaces = (tmp_path / "first workspace", tmp_path / "second workspace")
    for ws in workspaces:
        ws.mkdir()
        (ws / "notes.md").write_text("A supplied decision\n")
    gy.user_path().write_text(gy.dump({"version": 1, "roles": {
        role: {"harness": "native", "model_id": f"{role}/model"}
        for role in ("scout", "planner", "coder", "reviewer")}}))
    return build_context(DashboardConfig(port=PORT, token=TOKEN, allow_run=False,
                                         sessions_dir=SessionStore().root, workspaces=workspaces))


@pytest.mark.parametrize("starter,field", [
    ("plan-change", "goal"), ("plan-feedback", "feedback"),
    ("build-review", "goal"), ("run-with-role", "goal"), ("ask-role", "question"),
])
def test_http_preview_matches_cli_without_writes_or_execution(context, monkeypatch, capsys, starter, field):
    """Transport must not lose fields, choose another workspace, or activate a run."""
    _forbid_execution(monkeypatch)
    roots = (context.workspace, context.store.root, gy.user_path().parent,
             SessionStore().root, type(context.workspace)(os.environ["GARUDA_LEASES_DIR"]))
    before = _snapshot(*roots)
    text = "Keep Unicode café and shell data ' ; $(touch unintended)"
    inputs = {field: text, "sources": ["notes.md"]}
    argv = ["starter", "run", starter, "--preview", "--json",
            "--workspace", str(context.workspace), "--" + field, text, "--source", "notes.md"]
    if starter != "ask-role":
        inputs["constraints"] = "Keep retry behavior"
        argv += ["--constraints", inputs["constraints"]]
    args = build_parser().parse_args(argv)
    assert scenario_cli.run(args) == 0
    cli = json.loads(capsys.readouterr().out)
    response = _call(context, "/api/scenarios/preview", payload={"starter_id": starter, "inputs": inputs})
    assert response.status == 200, _body(response)
    http = _body(response)
    assert http["plan"]["digest"] == cli["plan"]["digest"]
    assert http["plan"]["equivalent_command"] == cli["plan"]["equivalent_command"]
    assert http["readiness"] == cli["readiness"]
    assert _snapshot(*roots) == before


def test_read_only_library_and_detail_use_installed_metadata(context, monkeypatch):
    _forbid_execution(monkeypatch)
    before = _snapshot(context.workspace, context.store.root, gy.user_path().parent)
    response = _call(context, "/api/scenarios")
    assert response.status == 200
    rows = _body(response)["starters"]
    assert {row["id"] for row in rows} == {"plan-change", "plan-feedback", "build-review", "run-with-role", "ask-role"}
    for row in rows:
        detail = _call(context, "/api/scenarios/" + row["id"])
        assert detail.status == 200
        data = _body(detail)
        assert data["fields"] == row["fields"]
        assert data["readiness"] == row["readiness"]
        assert data["example_inputs"]
    assert _snapshot(context.workspace, context.store.root, gy.user_path().parent) == before


@pytest.mark.parametrize("assembly", ["startup", "live-context"])
def test_workspace_selection_is_an_allowlisted_index_in_read_only_mode(context, monkeypatch, assembly):
    if assembly == "live-context":
        from garuda.interfaces.web.live import LiveRuns

        loop = asyncio.new_event_loop()
        try:
            context.live = LiveRuns(loop=loop, store=context.store, workspaces=context.workspaces)
            context.workspaces = ()  # Existing embedders attach the live owner directly.
        finally:
            loop.close()  # Only pure HTTP reads are exercised; no loop work is submitted.
    _forbid_execution(monkeypatch)
    config = _body(_call(context, "/api/config"))
    assert len(config["workspaces"]) == 2
    second = config["workspaces"][1]["path"]
    response = _call(context, "/api/scenarios/preview", payload={
        "starter_id": "plan-change", "workspace": 1, "inputs": {"goal": "use the selected workspace"}})
    assert response.status == 200
    assert _body(response)["plan"]["workspace"] == second
    for selection in (True, -1, 2, second, "1", None):
        response = _call(context, "/api/scenarios/preview", payload={
            "starter_id": "plan-change", "workspace": selection, "inputs": {"goal": "refuse"}})
        assert response.status == 400
    assert _call(context, "/api/scenarios", query="workspace=0&workspace=1").status == 400
    if assembly == "live-context":
        context.live.workspaces = ()
        refused = _call(context, "/api/scenarios/preview", payload={
            "starter_id": "plan-change", "inputs": {"goal": "do not widen an empty allowlist"}})
        assert refused.status == 400 and _body(refused)["error"]["code"] == "invalid_request"
        assert _body(_call(context, "/api/config"))["workspaces"] == []


@pytest.mark.parametrize("extra", [
    {"runtime": "native"}, {"flow": {"steps": []}}, {"catalog": {}},
    {"allow_cross_project_context": True}, {"operation_id": "execute"},
])
def test_preview_body_cannot_supply_execution_authority(context, extra):
    response = _call(context, "/api/scenarios/preview", payload={
        "starter_id": "plan-change", "inputs": {"goal": "bounded request"}, **extra})
    assert response.status == 400
    assert _body(response)["error"]["code"] == "invalid_request"


@pytest.mark.parametrize("body", [
    b'[]', b'{"starter_id":"plan-change","workspace":0,"workspace":1,"inputs":{"goal":"ambiguous"}}',
    b'{"starter_id":"plan-change","inputs":{"goal":"first","goal":"second"}}',
])
def test_preview_rejects_ambiguous_json_before_compilation(context, body):
    response = dispatch(Request("POST", "/api/scenarios/preview", body=body, headers={
        "host": f"127.0.0.1:{PORT}", "origin": f"http://127.0.0.1:{PORT}", TOKEN_HEADER: TOKEN}), context)
    assert response.status == 400


def test_sources_are_authorized_against_the_selected_workspace(context, monkeypatch):
    _forbid_execution(monkeypatch)
    response = _call(context, "/api/scenarios/preview", payload={"starter_id": "ask-role", "inputs": {
        "question": "read an unselected file", "sources": ["../second workspace/notes.md"]}})
    assert response.status == 400
    assert _body(response)["error"]["code"] == "starter.source_escapes"


@pytest.mark.parametrize("headers,status", [
    ({TOKEN_HEADER: "wrong"}, 401), ({"host": "rebound.test:8787"}, 403),
    ({"origin": "http://elsewhere.test"}, 403), ({"origin": ""}, 403),
])
def test_preview_uses_existing_transport_security(context, headers, status):
    response = _call(context, "/api/scenarios/preview", payload={
        "starter_id": "plan-change", "inputs": {"goal": "protected preview"}}, headers=headers)
    assert response.status == status


def test_result_route_uses_actual_owned_flow_records_without_launch(request, monkeypatch):
    fixture = request.getfixturevalue("completed_plan")
    ctx = build_context(DashboardConfig(port=PORT, token=TOKEN, allow_run=False,
                                        sessions_dir=fixture["store"].root, workspaces=(fixture["ws"],)))
    _forbid_execution(monkeypatch)
    before = _snapshot(fixture["ws"], fixture["store"].root, gy.user_path().parent)
    response = _call(ctx, "/api/scenario-runs/" + fixture["sid"])
    assert response.status == 200
    result = _body(response)
    assert result["session_id"] == fixture["sid"]
    assert result["coverage"]["complete"]
    assert result["verification"]["status"] == "unavailable"
    assert result["next_action"]["plan_artifact"] == fixture["reference"]
    page = _body(_call(ctx, "/api/scenario-runs/" + fixture["sid"], query="limit=1&offset=0"))
    assert not page["coverage"]["complete"] and page["coverage"]["returned_records"] == 1
    assert page["next_action"]["id"] == "inspect-records"
    for query in ("limit=0", "limit=201", "offset=-1", "offset=invalid", "limit=1&limit=2"):
        assert _call(ctx, "/api/scenario-runs/" + fixture["sid"], query=query).status == 400
    assert _call(ctx, "/api/scenarios/start", payload={"starter_id": "plan-change"}).status in (404, 405)
    assert _snapshot(fixture["ws"], fixture["store"].root, gy.user_path().parent) == before
