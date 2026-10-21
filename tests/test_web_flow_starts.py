"""Durable dashboard flow admission at the HTTP and real Linux child boundary."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import parse_qs
from urllib.request import Request as URLRequest
from urllib.request import urlopen

import pytest
import yaml

from garuda.config import garuda_yaml as gy
from garuda.flows.engine import FlowStopped
from garuda.flows.service import FlowExecutionService
from garuda.interfaces.web import DashboardConfig, build_context
from garuda.interfaces.web import http as dashboard_http
from garuda.interfaces.web.routes import dispatch
from garuda.interfaces.web.security import TOKEN_HEADER
from garuda.interfaces.web.wire import Request
from garuda.scenarios.gateway import FlowStartGateway
from tests.test_scenarios import completed_plan as completed_plan

PORT = 8787
TOKEN = "owned-flow-start-test"
LINUX = pytest.mark.skipif(sys.platform != "linux", reason="actual Linux-owned flow child")


def _request(path, *, method="GET", body=None, headers=None):
    request_headers = {"host": f"127.0.0.1:{PORT}", TOKEN_HEADER: TOKEN}
    if method != "GET":
        request_headers["origin"] = f"http://127.0.0.1:{PORT}"
    request_headers.update(headers or {})
    return Request(method, path, query=parse_qs(""), headers=request_headers,
                   body=json.dumps(body).encode() if body is not None else b"")


async def _call(ctx, path, *, method="GET", body=None, headers=None):
    return await asyncio.to_thread(
        dispatch, _request(path, method=method, body=body, headers=headers), ctx)


def _body(response):
    return json.loads(response.body)


def _http_json(base, path, *, method="GET", body=None, token=TOKEN,
               origin=None):
    raw = json.dumps(body).encode() if body is not None else None
    headers = {TOKEN_HEADER: token}
    if method != "GET":
        headers["Origin"] = origin or base.rstrip("/")
        headers["Content-Type"] = "application/json"
    request = URLRequest(base + path.lstrip("/"), data=raw, headers=headers,
                         method=method)
    try:
        response = urlopen(request, timeout=30)
    except HTTPError as exc:
        return exc.code, json.loads(exc.read())
    with response:
        return response.status, json.loads(response.read())


def _disconnect_post(port, path, body):
    """Deliver a complete request, then close before the response owns anything."""
    raw = json.dumps(body).encode()
    request = (
        f"POST {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
        f"Origin: http://127.0.0.1:{port}\r\n{TOKEN_HEADER}: {TOKEN}\r\n"
        f"Content-Type: application/json\r\nContent-Length: {len(raw)}\r\n"
        "Connection: close\r\n\r\n"
    ).encode() + raw
    with socket.create_connection(("127.0.0.1", port), timeout=5) as client:
        client.sendall(request)


def _configure_native_flow(workspace: Path) -> Path:
    profile = workspace / ".agent" / "agents" / "owned-flow.yaml"
    profile.parent.mkdir(parents=True)
    profile.write_text(
        "name: owned-flow\ntools: []\npermission_mode: smart\n"
        "enable_verifier: false\nmax_turns: 2\n")
    gy.user_path().write_text(gy.dump({
        "version": 1,
        "roles": {role: {"harness": "native", "agent": "owned-flow"}
                  for role in ("scout", "planner", "coder", "reviewer")},
    }))
    return profile


@pytest.fixture
async def write_context(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    subprocess.run(["git", "init", "-q", str(workspace)], check=True)
    (workspace / "source.txt").write_text("previewed source\n")
    _configure_native_flow(workspace)
    monkeypatch.setattr(sys, "platform", "linux")
    ctx = build_context(DashboardConfig(
        port=PORT, token=TOKEN, allow_run=True, sessions_dir=tmp_path / "sessions",
        workspaces=(workspace,), open_browser=False), loop=asyncio.get_running_loop())
    try:
        yield ctx
    finally:
        await ctx.flow_starts.aclose()
        await ctx.live.aclose()


async def _preview(ctx, starter="plan-change", inputs=None):
    response = await _call(ctx, "/api/scenarios/preview", method="POST", body={
        "starter_id": starter, "workspace": 0,
        "inputs": inputs or {"goal": "produce one bounded plan"},
    })
    assert response.status == 200, _body(response)
    return _body(response)


def _start_body(preview, operation_id, *, starter="plan-change", inputs=None):
    return {
        "operation_id": operation_id,
        "starter_id": starter,
        "inputs": inputs or {"goal": "produce one bounded plan"},
        "workspace": 0,
        "preview_digest": preview["plan"]["digest"],
        "authority_digest": preview["authority"]["digest"],
    }


async def _status(ctx, operation_id, wanted, *, timeout=10):
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        response = await _call(ctx, f"/api/scenario-operations/{operation_id}")
        assert response.status == 200, _body(response)
        payload = _body(response)
        if payload["status"] in wanted:
            return payload
        assert asyncio.get_running_loop().time() < deadline, payload
        await asyncio.sleep(.02)


async def test_concurrent_posts_schedule_once_and_restart_never_replays(
        write_context, monkeypatch):
    """The operation store, rather than HTTP timing, chooses the only dispatcher."""
    ctx = write_context
    ctx.flow_starts.admission_wait = .05
    preview = await _preview(ctx)
    operation_id = str(uuid.uuid4())
    request = _start_body(preview, operation_id)
    entered = asyncio.Event()
    calls = 0

    async def long_flow(_service, *_args, **_kwargs):
        nonlocal calls
        calls += 1
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(FlowExecutionService, "run_prepared", long_flow)
    posts = [asyncio.create_task(_call(
        ctx, "/api/scenarios/start", method="POST", body=request)) for _ in range(2)]
    await asyncio.wait_for(entered.wait(), 2)
    responses = await asyncio.gather(*posts)
    payloads = [_body(response) for response in responses]
    assert all(response.status in {201, 202} for response in responses)
    assert calls == 1
    assert sorted(bool(payload.get("new")) for payload in payloads) == [False, True]
    assert any(payload.get("duplicate") for payload in payloads)

    conflict = await _call(ctx, "/api/scenarios/start", method="POST", body={
        **request, "inputs": {"goal": "a different operation"}})
    assert conflict.status == 409
    assert _body(conflict)["error"]["code"] == "starter.operation_conflict"

    old = ctx.flow_starts
    await old.aclose()
    record = old.intents.read(operation_id)
    assert record["phase"] == "uncertain" and record["reason"] == "server.shutdown"

    ctx.flow_starts = FlowStartGateway(
        store=ctx.store, workspaces=ctx.workspaces, max_permission="smart",
        approvals=ctx.live.approvals, admission_wait=.05)
    duplicate = await _call(ctx, "/api/scenarios/start", method="POST", body=request)
    assert duplicate.status == 200
    assert _body(duplicate)["status"] == "uncertain"
    assert calls == 1


async def test_restart_projects_a_dead_owner_with_running_metadata_as_uncertain(
        write_context, monkeypatch):
    """A durable parent cannot make a dead server-owned task look live after restart."""
    ctx = write_context
    entered = asyncio.Event()

    async def held_child(_launch):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr("garuda.flows.launch.launch_step", held_child)
    preview = await _preview(ctx)
    operation_id = str(uuid.uuid4())
    response = await _call(
        ctx, "/api/scenarios/start", method="POST",
        body=_start_body(preview, operation_id))
    assert response.status == 201
    await asyncio.wait_for(entered.wait(), 2)

    old = ctx.flow_starts
    projection = FlowStartGateway(
        store=ctx.store, workspaces=ctx.workspaces, max_permission="smart",
        approvals=ctx.live.approvals)
    monkeypatch.setattr("garuda.scenarios.gateway.owner_liveness", lambda _owner: False)
    ctx.flow_starts = projection
    try:
        status = await _call(ctx, f"/api/scenario-operations/{operation_id}")
        assert status.status == 200
        assert _body(status)["status"] == "uncertain"
        assert _body(status)["reason"] == "owner.uncertain"
    finally:
        await projection.aclose()
        ctx.flow_starts = old


async def test_operation_status_refuses_an_allowlisted_path_replaced_by_a_symlink(
        write_context, monkeypatch):
    ctx = write_context
    ctx.flow_starts.admission_wait = .02

    async def held_flow(_service, *_args, **_kwargs):
        await asyncio.Event().wait()

    monkeypatch.setattr(FlowExecutionService, "run_prepared", held_flow)
    preview = await _preview(ctx)
    operation_id = str(uuid.uuid4())
    response = await _call(
        ctx, "/api/scenarios/start", method="POST",
        body=_start_body(preview, operation_id))
    assert response.status in {201, 202}

    workspace = ctx.workspaces[0]
    moved = workspace.with_name("moved-workspace")
    workspace.rename(moved)
    workspace.symlink_to(moved, target_is_directory=True)
    try:
        status = await _call(ctx, f"/api/scenario-operations/{operation_id}")
        assert status.status == 403
        assert _body(status)["error"]["code"] == "starter.operation_scope_changed"
    finally:
        workspace.unlink()
        moved.rename(workspace)


@pytest.mark.parametrize("changed", ["repository", "config", "profile", "runtime-settings"])
async def test_changed_admitted_authority_stops_before_the_first_child(
        write_context, monkeypatch, changed):
    """The second source check is after durable parent admission and before a role child."""
    ctx = write_context
    workspace = ctx.workspaces[0]
    inputs = {"goal": "use the exact admitted inputs", "sources": ["source.txt"]}
    preview = await _preview(ctx, inputs=inputs)
    assert preview["authority"]["can_start"]
    operation_id = str(uuid.uuid4())
    request = _start_body(preview, operation_id, inputs=inputs)
    profile = workspace / ".agent" / "agents" / "owned-flow.yaml"
    targets = {
        "repository": workspace / "source.txt",
        "config": gy.user_path(),
        "profile": profile,
        "runtime-settings": Path(os.environ["GARUDA_GLOBAL_SETTINGS"]),
    }
    target = targets[changed].absolute()
    original = FlowExecutionService.run_prepared

    async def change_after_authorization(service, plan, context, **kwargs):
        assert any(Path(fact.path) == target for fact in context.source_facts)
        target.write_text("changed after durable launch authorization\n")
        return await original(service, plan, context, **kwargs)

    async def no_child(_launch):
        raise AssertionError("changed authority launched a role child")

    monkeypatch.setattr(FlowExecutionService, "run_prepared", change_after_authorization)
    monkeypatch.setattr("garuda.flows.launch.launch_step", no_child)
    response = await _call(ctx, "/api/scenarios/start", method="POST", body=request)
    assert response.status == 201, _body(response)
    terminal = await _status(ctx, operation_id, {"terminal"})
    assert terminal["outcome"] == "stopped"
    meta = ctx.store.load_meta(terminal["session_id"])
    assert meta["flow_stop"]["code"] == "flow.authority_changed"
    assert meta["publication_policy"]["kind"] == "owned-flow"
    assert not [row for row in ctx.store.list_sessions() if row.get("flow_step")]
    with pytest.raises(FlowStopped) as caught:
        await FlowExecutionService(ctx.store).resume(terminal["session_id"])
    assert caught.value.code == "flow.operation_managed"


async def test_replaced_workspace_after_authorization_stops_before_parent_or_child(
        write_context, monkeypatch):
    ctx = write_context
    workspace = ctx.workspaces[0]
    preview = await _preview(ctx)
    operation_id = str(uuid.uuid4())
    request = _start_body(preview, operation_id)
    original = FlowExecutionService.run_prepared
    moved = workspace.with_name("authorized-workspace")

    async def replace_after_authorization(service, plan, context, **kwargs):
        workspace.rename(moved)
        workspace.mkdir()
        return await original(service, plan, context, **kwargs)

    async def no_child(_launch):
        raise AssertionError("a replacement workspace launched a role child")

    monkeypatch.setattr(FlowExecutionService, "run_prepared", replace_after_authorization)
    monkeypatch.setattr("garuda.flows.launch.launch_step", no_child)
    try:
        response = await _call(ctx, "/api/scenarios/start", method="POST", body=request)
        assert response.status == 201
        assert _body(response)["status"] == "refused"
        record = ctx.flow_starts.intents.read(operation_id)
        assert record["phase"] == "refused"
        assert record["reason"] == "flow.workspace_changed"
        assert not ctx.store.session_dir(record["reserved_session"]).exists()
    finally:
        if workspace.exists():
            workspace.rmdir()
        if moved.exists():
            moved.rename(workspace)


async def test_busy_workspace_refuses_without_a_parent_and_never_relaunches(
        write_context, monkeypatch):
    """A lease conflict before the parent exists is a proved refusal, not uncertainty."""
    from garuda.interfaces.run_guard import WorkspaceLeaseGuard

    ctx = write_context
    preview = await _preview(ctx)
    operation_id = str(uuid.uuid4())
    request = _start_body(preview, operation_id)

    async def no_child(_launch):
        raise AssertionError("a busy workspace launched a role child")

    monkeypatch.setattr("garuda.flows.launch.launch_step", no_child)
    holder = WorkspaceLeaseGuard(str(ctx.workspaces[0]), str(uuid.uuid4()), mode="mutating")
    holder.acquire()
    try:
        response = await _call(ctx, "/api/scenarios/start", method="POST", body=request)
        assert response.status == 201, _body(response)
        assert _body(response)["status"] == "refused"
        assert _body(response)["reason"] == "workspace.busy"
    finally:
        await holder.release()
    record = ctx.flow_starts.intents.read(operation_id)
    assert record["phase"] == "refused"
    assert not ctx.store.session_dir(record["reserved_session"]).exists()

    calls = 0

    async def counted(_service, *_args, **_kwargs):
        nonlocal calls
        calls += 1

    monkeypatch.setattr(FlowExecutionService, "run_prepared", counted)
    duplicate = await _call(ctx, "/api/scenarios/start", method="POST", body=request)
    assert duplicate.status == 200
    assert _body(duplicate)["status"] == "refused" and _body(duplicate)["duplicate"]
    assert calls == 0


async def test_changed_plan_artifact_stops_after_admission_without_a_child(
        completed_plan, monkeypatch):  # noqa: F811 - imported pytest fixture
    source = completed_plan
    _configure_native_flow(source["ws"])
    monkeypatch.setattr(sys, "platform", "linux")
    ctx = build_context(DashboardConfig(
        port=PORT, token=TOKEN, allow_run=True, sessions_dir=source["store"].root,
        workspaces=(source["ws"],), open_browser=False), loop=asyncio.get_running_loop())
    inputs = {"feedback": "apply this feedback", "plan_artifact": source["reference"]}
    original = FlowExecutionService.run_prepared

    async def change_artifact(service, plan, context, **kwargs):
        artifact = source["artifact"].absolute()
        assert any(Path(fact.path) == artifact for fact in context.source_facts)
        artifact.chmod(0o600)
        artifact.write_text("changed after launch authorization\n")
        return await original(service, plan, context, **kwargs)

    async def no_child(_launch):
        raise AssertionError("changed plan artifact launched a role child")

    try:
        preview = await _preview(ctx, starter="plan-feedback", inputs=inputs)
        body = _start_body(preview, str(uuid.uuid4()),
                           starter="plan-feedback", inputs=inputs)
        monkeypatch.setattr(FlowExecutionService, "run_prepared", change_artifact)
        monkeypatch.setattr("garuda.flows.launch.launch_step", no_child)
        response = await _call(ctx, "/api/scenarios/start", method="POST", body=body)
        terminal = await _status(ctx, body["operation_id"], {"terminal"})
        assert response.status == 201
        assert ctx.store.load_meta(terminal["session_id"])["flow_stop"]["code"] == \
            "flow.authority_changed"
        assert not [row for row in ctx.store.list_sessions()
                    if row.get("flow_step", {}).get("flow_session") == terminal["session_id"]]
    finally:
        await ctx.flow_starts.aclose()
        await ctx.live.aclose()


async def test_stale_preview_and_permission_escalation_refuse_before_intent(
        write_context):
    ctx = write_context
    preview = await _preview(ctx)
    body = _start_body(preview, str(uuid.uuid4()))
    config = gy.load_file(gy.user_path())
    config["roles"]["planner"]["model_id"] = "changed/model"
    gy.user_path().write_text(gy.dump(config))
    stale = await _call(ctx, "/api/scenarios/start", method="POST", body=body)
    assert stale.status == 409
    assert _body(stale)["error"]["code"] == "starter.preview_changed"
    assert not (ctx.store.root / "launch-intents").exists()

    config["roles"]["coder"]["permissions"] = "yolo"
    config["flows"] = {"plan-build-review": {
        "steps": [{"id": "build", "role": "coder", "outputs": ["patch"]}]}}
    gy.user_path().write_text(gy.dump(config))
    elevated = await _preview(ctx, starter="build-review",
                              inputs={"goal": "do bounded work"})
    assert elevated["readiness"]["can_run"]
    assert elevated["authority"]["can_start"] is False
    assert elevated["authority"]["diagnostics"][0]["code"] == \
        "starter.permission_exceeds_ceiling"
    denied = await _call(ctx, "/api/scenarios/start", method="POST", body=_start_body(
        elevated, str(uuid.uuid4()), starter="build-review",
        inputs={"goal": "do bounded work"}))
    assert denied.status == 409
    assert _body(denied)["error"]["code"] == "starter.permission_exceeds_ceiling"
    assert not (ctx.store.root / "launch-intents").exists()


async def test_owned_background_start_keeps_transport_guards(write_context):
    ctx = write_context
    inputs = {"goal": "run in the background", "role": "coder", "options": {"bg": True}}
    preview = await _preview(ctx, starter="run-with-role", inputs=inputs)
    assert preview["plan"]["kind"] == "run"
    assert preview["authority"]["can_start"] is True
    request = _start_body(preview, str(uuid.uuid4()), starter="run-with-role", inputs=inputs)

    attempts = [
        ({TOKEN_HEADER: "wrong"}, 401, "unauthorized"),
        ({"origin": "http://attacker.invalid"}, 403, "forbidden_origin"),
        ({"host": f"attacker.invalid:{PORT}"}, 403, "forbidden_host"),
    ]
    for headers, status, code in attempts:
        response = await _call(ctx, "/api/scenarios/start", method="POST",
                               body=request, headers=headers)
        assert response.status == status
        assert _body(response)["error"]["code"] == code
        assert not (ctx.store.root / "launch-intents").exists()
        assert ctx.store.list_sessions() == []


def _local_model(*, block=False):
    requests = []
    lock = threading.Lock()
    started = threading.Event()
    release = threading.Event()

    class Endpoint(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            with lock:
                requests.append(body)
                index = len(requests)
            started.set()
            if block:
                release.wait(30)
            kind = "notes" if index % 2 else "plan"
            content = (f'<garuda-artifact type="{kind}">\n'
                       f"owned {kind} output\n</garuda-artifact>")
            response = {
                "id": "owned-start-fixture", "object": "chat.completion", "created": 1,
                "model": "owned-start", "choices": [{"index": 0,
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }
            raw = json.dumps(response).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            try:
                self.wfile.write(raw)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Endpoint)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, requests, started, release


@LINUX
async def test_gateway_shutdown_closes_an_active_owned_child(tmp_path, monkeypatch):
    """Gateway shutdown reaches the real supervisor before releasing the flow lease."""
    monkeypatch.setenv("OPENAI_API_KEY", "local-protocol-fixture")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    subprocess.run(["git", "init", "-q", str(workspace)], check=True)
    subprocess.run(["git", "-C", str(workspace), "-c", "user.name=Test",
                    "-c", "user.email=test@example.invalid", "commit", "--allow-empty",
                    "-qm", "base"], check=True)
    _configure_native_flow(workspace)
    server, _requests, started, release = _local_model(block=True)
    Path(os.environ["GARUDA_GLOBAL_SETTINGS"]).write_text(yaml.safe_dump({
        "models": {"owned": {"model": "openai/owned-start",
                              "api_base": f"http://127.0.0.1:{server.server_port}/v1"}},
        "model_bindings": {"default": {"reasoning": "owned"}},
        "capacity": {"native": 1},
    }))
    ctx = build_context(DashboardConfig(
        port=PORT, token=TOKEN, allow_run=True, sessions_dir=tmp_path / "sessions",
        workspaces=(workspace,), open_browser=False), loop=asyncio.get_running_loop())
    try:
        preview = await _preview(ctx)
        operation_id = str(uuid.uuid4())
        post = asyncio.create_task(_call(
            ctx, "/api/scenarios/start", method="POST",
            body=_start_body(preview, operation_id)))
        assert await asyncio.to_thread(started.wait, 20), "owned model child did not start"
        await ctx.flow_starts.aclose()
        response = await asyncio.wait_for(post, 10)
        assert response.status in {201, 202}
        projection = await ctx.flow_starts.status(operation_id)
        assert projection["status"] == "terminal"
        assert projection["outcome"] == "interrupted"
        meta = ctx.store.load_meta(projection["session_id"])
        assert meta["flow_state"] == "interrupted"

        from garuda.interfaces.run_guard import WorkspaceLeaseGuard

        lease = WorkspaceLeaseGuard(workspace, str(uuid.uuid4()), mode="mutating")
        lease.acquire()
        await lease.release()
    finally:
        release.set()
        await ctx.flow_starts.aclose()
        await ctx.live.aclose()
        server.shutdown()
        server.server_close()


@LINUX
async def test_real_http_start_runs_one_owned_flow_for_duplicate_requests(tmp_path, monkeypatch):
    """A real HTTP client disconnect cannot own or duplicate the native children."""
    monkeypatch.setenv("OPENAI_API_KEY", "local-protocol-fixture")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    subprocess.run(["git", "init", "-q", str(workspace)], check=True)
    subprocess.run(["git", "-C", str(workspace), "-c", "user.name=Test",
                    "-c", "user.email=test@example.invalid", "commit", "--allow-empty",
                    "-qm", "base"], check=True)
    _configure_native_flow(workspace)
    server, requests, _started, _release = _local_model()
    settings = Path(os.environ["GARUDA_GLOBAL_SETTINGS"])
    settings.write_text(yaml.safe_dump({
        "models": {"owned": {"model": "openai/owned-start",
                              "api_base": f"http://127.0.0.1:{server.server_port}/v1"}},
        "model_bindings": {"default": {"reasoning": "owned"}},
        "capacity": {"native": 1},
    }))
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        dashboard_port = probe.getsockname()[1]
    ctx = build_context(DashboardConfig(
        port=dashboard_port, token=TOKEN, allow_run=True,
        sessions_dir=tmp_path / "sessions",
        workspaces=(workspace,), open_browser=False), loop=asyncio.get_running_loop())
    dashboard, dashboard_port = dashboard_http.bind(ctx, dashboard_port)
    dashboard.daemon_threads = True
    threading.Thread(target=dashboard.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{dashboard_port}/"
    try:
        status, preview = await asyncio.to_thread(
            _http_json, base, "/api/scenarios/preview", method="POST",
            body={"starter_id": "plan-change", "workspace": 0,
                  "inputs": {"goal": "produce one bounded plan"}})
        assert status == 200, preview
        assert preview["authority"]["can_start"]
        operation_id = str(uuid.uuid4())
        body = _start_body(preview, operation_id)
        first, second = await asyncio.gather(*[
            asyncio.to_thread(_http_json, base, "/api/scenarios/start",
                              method="POST", body=body) for _ in range(2)])
        assert {first[0], second[0]} <= {200, 201, 202}
        deadline = asyncio.get_running_loop().time() + 60
        while True:
            status, terminal = await asyncio.to_thread(
                _http_json, base, f"/api/scenario-operations/{operation_id}")
            assert status == 200, terminal
            if terminal["status"] == "terminal":
                break
            assert asyncio.get_running_loop().time() < deadline, terminal
            await asyncio.sleep(.02)
        assert terminal["outcome"] == "completed"
        assert len(requests) == 2
        parent = ctx.store.load_meta(terminal["session_id"])
        assert parent["flow_state"] == "completed"
        assert parent["publication_policy"]["kind"] == "owned-flow"
        children = [row for row in ctx.store.list_sessions()
                    if row.get("flow_step", {}).get("flow_session") == terminal["session_id"]]
        assert {row["flow_step"]["step"] for row in children} == {"scout", "plan"}

        replay_status, replay = await asyncio.to_thread(
            _http_json, base, "/api/scenarios/start", method="POST", body=body)
        assert replay_status == 200
        assert replay["session_id"] == terminal["session_id"]
        await asyncio.sleep(.1)
        assert len(requests) == 2

        disconnected_id = str(uuid.uuid4())
        disconnected = _start_body(preview, disconnected_id)
        await asyncio.to_thread(
            _disconnect_post, dashboard_port, "/api/scenarios/start", disconnected)
        deadline = asyncio.get_running_loop().time() + 60
        while True:
            status, terminal = await asyncio.to_thread(
                _http_json, base, f"/api/scenario-operations/{disconnected_id}")
            if status == 200 and terminal["status"] == "terminal":
                break
            assert status in {200, 404} and asyncio.get_running_loop().time() < deadline, \
                terminal
            await asyncio.sleep(.02)
        assert terminal["outcome"] == "completed" and len(requests) == 4

        denied_status, denied = await asyncio.to_thread(
            _http_json, base, "/api/scenarios/start", method="POST", body=body,
            token="wrong")
        assert denied_status == 401 and denied["error"]["code"] == "unauthorized"
        denied_status, denied = await asyncio.to_thread(
            _http_json, base, "/api/scenarios/start", method="POST", body=body,
            origin="http://attacker.invalid")
        assert denied_status == 403 and denied["error"]["code"] == "forbidden_origin"
    finally:
        dashboard.shutdown()
        dashboard.server_close()
        await ctx.flow_starts.aclose()
        await ctx.live.aclose()
        server.shutdown()
        server.server_close()


@pytest.mark.parametrize("has_checks", [False, True])
async def test_large_configuration_snapshot_only_bounds_requested_checks(
        write_context, monkeypatch, has_checks):
    from garuda.scenarios.compile import compile_with_context
    from tests.test_web_run_starts import _capture

    ctx = write_context
    workspace = ctx.allowed_workspaces[0]
    profile = workspace / ".agent" / "agents" / "owned-flow.yaml"
    files = [f"instructions-{index}.md" for index in range(33)]
    for name in files:
        (profile.parent / name).write_text("One reviewed project instruction.\n")
    profile.write_text(yaml.safe_dump({
        "version": 1, "name": "owned-flow", "tools": {"preset": "none"},
        "instructions": {"files": files}}))
    config = gy.load_file(gy.user_path())
    config["flows"] = {"plan-build-review": {"steps": [{"id": "build", "role": "coder"}]}}
    if has_checks:
        config["checks"] = [{"run": "true"}]
    gy.user_path().write_text(gy.dump(config))
    inputs = {"goal": "use a bounded configuration without inventing check authority"}
    _plan, context = compile_with_context("build-review", inputs, workspace, store=ctx.store)
    assert len(context.configuration_sources) > 32
    launches = _capture(monkeypatch)
    preview = await _preview(ctx, starter="build-review", inputs=inputs)
    assert preview["authority"]["can_start"], preview["authority"]["diagnostics"]
    operation = str(uuid.uuid4())
    response = await _call(ctx, "/api/scenarios/start", method="POST", body=_start_body(
        preview, operation, starter="build-review", inputs=inputs))
    assert response.status in {201, 202}, _body(response)
    terminal = await _status(ctx, operation, {"terminal", "refused", "uncertain"})
    if has_checks:
        assert terminal["status"] == "refused", terminal
        assert terminal["reason"] == "flow.check_request_invalid"
        assert launches == []
    else:
        assert terminal["status"] == "terminal" and terminal["outcome"] == "stopped", terminal
        assert len(launches) == 1
        assert ctx.store.load_meta(terminal["session_id"])["flow_stop"]["code"] == "test.captured"


@LINUX
async def test_checked_flow_start_projects_its_committed_verification(tmp_path, monkeypatch):
    """A checked start publishes under the verification policy and still projects terminal."""
    monkeypatch.setenv("OPENAI_API_KEY", "local-protocol-fixture")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    subprocess.run(["git", "init", "-q", str(workspace)], check=True)
    subprocess.run(["git", "-C", str(workspace), "-c", "user.name=Test",
                    "-c", "user.email=test@example.invalid", "commit", "--allow-empty",
                    "-qm", "base"], check=True)
    _configure_native_flow(workspace)
    config = gy.load_file(gy.user_path())
    config["flows"] = {"plan-build-review": {"steps": [{"id": "build", "role": "coder"}]}}
    config["checks"] = [{"run": "true"}]
    gy.user_path().write_text(gy.dump(config))
    server, requests, _started, _release = _local_model()
    Path(os.environ["GARUDA_GLOBAL_SETTINGS"]).write_text(yaml.safe_dump({
        "models": {"owned": {"model": "openai/owned-start",
                              "api_base": f"http://127.0.0.1:{server.server_port}/v1"}},
        "model_bindings": {"default": {"reasoning": "owned"}},
        "capacity": {"native": 1},
    }))
    ctx = build_context(DashboardConfig(
        port=PORT, token=TOKEN, allow_run=True, sessions_dir=tmp_path / "sessions",
        workspaces=(workspace,), open_browser=False), loop=asyncio.get_running_loop())
    try:
        inputs = {"goal": "build one checked change"}
        preview = await _preview(ctx, starter="build-review", inputs=inputs)
        assert preview["authority"]["can_start"], preview["authority"]["diagnostics"]
        operation_id = str(uuid.uuid4())
        response = await _call(ctx, "/api/scenarios/start", method="POST", body=_start_body(
            preview, operation_id, starter="build-review", inputs=inputs))
        assert response.status in {201, 202}, _body(response)
        terminal = await _status(ctx, operation_id, {"terminal", "uncertain", "quarantined"},
                                 timeout=60)
        assert terminal["status"] == "terminal" and terminal["outcome"] == "completed", terminal
        parent = ctx.store.load_meta(terminal["session_id"])
        assert parent["publication_policy"]["kind"] == "flow-verification"
        assert parent["state"]["verification"]["status"] == "passed"
        assert ctx.flow_starts.intents.read(operation_id)["phase"] == "terminal"
    finally:
        await ctx.flow_starts.aclose()
        await ctx.live.aclose()
        server.shutdown()
        server.server_close()
