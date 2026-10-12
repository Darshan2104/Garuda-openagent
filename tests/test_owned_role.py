"""Real native tool and ACP protocol execution retain ownership through ECHILD."""

import asyncio
import json
import os
import shlex
import signal
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import yaml

from garuda.agents import role_agent
from garuda.core.sessions import SessionStore
from garuda.flows.engine import StepLaunch
from garuda.interfaces.run_guard import WorkspaceLeaseGuard
from garuda.runtime.capacity import CapacityStore, CapacityUnavailable
from garuda.runtime.owned_role import execute_owned_role, prepare_owned_step
from garuda.runtime.role_control import RoleControl
from garuda.runtime.roles import RolePlan
from garuda.workspace.lease import LeaseConflictError, LeaseStore

linux = pytest.mark.skipif(sys.platform != "linux", reason="actual Linux role ownership")

DETACHED = '''
import os,pathlib,signal,time
if os.fork() == 0:
    os.setsid()
    if os.fork() != 0: os._exit(0)
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    fd=os.open('/dev/null',os.O_RDWR)
    for stream in (0,1,2): os.dup2(fd,stream)
    pathlib.Path('helper').write_text(str(os.getpid()))
    while True:
        with open('writes','a') as handle: handle.write('x')
        time.sleep(.005)
'''


async def appears(path, task):
    deadline = asyncio.get_running_loop().time() + 20
    while not path.exists():
        if task.done():
            await task
            pytest.fail("owned role returned before its real writer started")
        assert asyncio.get_running_loop().time() < deadline, "owned role writer did not start"
        await asyncio.sleep(.01)


@pytest.fixture
async def case(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    subprocess.run(["git", "init", "-q", str(workspace)], check=True)
    leases = LeaseStore(tmp_path / "leases")
    parent = WorkspaceLeaseGuard(str(workspace), str(uuid.uuid4()), lease_store=leases, ttl_sec=1.2)
    parent.acquire()
    parent.start_heartbeat()
    store = SessionStore(tmp_path / "sessions")
    try:
        yield workspace, parent, leases, store
    finally:
        await parent.release()


def install_native(workspace, settings):
    requests = []
    release = workspace / "release"
    command = f"{shlex.quote(sys.executable)} -c {shlex.quote(DETACHED)}"

    class ModelEndpoint(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(body)
            if len(requests) == 1:
                message = {"role": "assistant", "content": None, "tool_calls": [
                    {"id": "owned-tool", "type": "function", "function": {
                        "name": "bash", "arguments": json.dumps({"command": command})}}]}
                finish = "tool_calls"
            else:
                deadline = time.monotonic() + 20
                while not release.exists() and time.monotonic() < deadline:
                    time.sleep(.01)
                message, finish = {"role": "assistant", "content": "native role completed"}, "stop"
            raw = json.dumps({"id": "local-model", "object": "chat.completion", "created": 1,
                              "model": "owned-fixture", "choices": [{"index": 0,
                              "message": message, "finish_reason": finish}],
                              "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}).encode()
            try:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), ModelEndpoint)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    settings.write_text(yaml.safe_dump({"models": {"owned": {"model": "openai/owned-fixture",
                         "api_base": f"http://127.0.0.1:{server.server_port}/v1"}},
                         "model_bindings": {"default": {"reasoning": "owned"}},
                         "capacity": {"native": 1}}))
    directory = workspace / ".agent" / "agents"
    directory.mkdir(parents=True)
    (directory / "owned.yaml").write_text(
        "name: owned\ntools: [bash]\npermission_mode: auto\nenable_verifier: false\n"
        "force_final_submission: false\nmax_turns: 3\n")
    return server, requests


def install_acp(workspace, settings):
    fixture = workspace.parent / "fixture_acp.py"
    fixture.write_text(
        "import os,sys,pathlib,time\n"
        f"sys.path.insert(0,{str(Path(__file__).resolve().parents[1])!r})\n"
        "from garuda.acp import fake_agent\n"
        "original=fake_agent._handle_prompt\n"
        "def prompt(*args,**kwargs):\n"
        f"    os.chdir({str(workspace)!r})\n"
        f"    exec({DETACHED!r})\n"
        "    while not pathlib.Path('release').exists(): time.sleep(.01)\n"
        "    return original(*args,**kwargs)\n"
        "fake_agent._handle_prompt=prompt\n"
        "raise SystemExit(fake_agent.main(['--profile','success']))\n")
    settings.write_text(yaml.safe_dump({"runtimes": [{"runtime_id": "owned-acp", "kind": "acp",
                         "command": [sys.executable, str(fixture)], "version": "1"}],
                         "capacity": {"owned-acp": 1}}))


@linux
@pytest.mark.parametrize("kind", ["native", "acp"])
@pytest.mark.parametrize("finish", ["complete", "cancel"])
async def test_actual_role_reaps_detached_writer_before_releasing_capacity(
        case, monkeypatch, kind, finish):
    workspace, parent, leases, store = case
    settings = Path(os.environ["GARUDA_GLOBAL_SETTINGS"])
    server = None
    helper_fd = None
    task = None
    released = asyncio.Event()
    dispatch = RoleControl._dispatch

    async def observe_release(control, operation, value):
        response = await dispatch(control, operation, value)
        if operation == "release":
            released.set()
        return response

    monkeypatch.setattr(RoleControl, "_dispatch", observe_release)
    try:
        if kind == "native":
            monkeypatch.setenv("OPENAI_API_KEY", "local-protocol-fixture")
            server, requests = install_native(workspace, settings)
            role = role_agent.bind(
                RolePlan("builder", "native", "native", permissions="auto", profile="owned"), str(workspace))
        else:
            install_acp(workspace, settings)
            role = RolePlan("builder", "owned-acp", "acp", permissions="auto")
        capability = parent.delegate("owned-test-child")
        launch = StepLaunch(parent.session_id, "build", "builder", role, "run the fixture",
                            str(workspace), capability, 1, False, store)
        request = prepare_owned_step(launch, timeout=30)
        if kind == "native":
            monkeypatch.setenv("GARUDA_REASONING_MODEL", "openai/wrong-model")
        task = asyncio.create_task(execute_owned_role(request, capability))
        await appears(workspace / "writes", task)
        helper_pid = int((workspace / "helper").read_text())
        helper_fd = os.pidfd_open(helper_pid)
        competitor = WorkspaceLeaseGuard(str(workspace), str(uuid.uuid4()), lease_store=leases)
        with pytest.raises(LeaseConflictError):
            competitor.acquire()
        slots = CapacityStore()
        with pytest.raises(CapacityUnavailable):
            slots.reserve(request.capacity_key, "real-competitor", 1)
        if finish == "cancel":
            task.cancel()
            await asyncio.sleep(.02)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            (workspace / "release").touch()
            await asyncio.wait_for(released.wait(), 10)
            # This is the real native/ACP facade's logical release. Its detached
            # TERM-resistant child is still inside the supervisor's cleanup grace.
            assert not task.done()
            with pytest.raises(CapacityUnavailable):
                slots.reserve(request.capacity_key, "real-competitor", 1)
            with pytest.raises(LeaseConflictError):
                competitor.acquire()
            result = await task
            assert result.success
            assert result.session_id == request.session_id
            assert result.closure["process"]["cleanup"] == {"backend": "linux-subreaper", "echild": True}
            assert result.closure["request_digest"] == request.digest
            assert "completed" in result.output or "done:" in result.output
        assert not Path(f"/proc/{helper_pid}").exists()
        size = (workspace / "writes").stat().st_size
        await asyncio.sleep(.1)
        assert (workspace / "writes").stat().st_size == size
        reservation = slots.reserve(request.capacity_key, "real-competitor", 1)
        slots.release(reservation)
        assert not parent._quarantined
        with pytest.raises(LeaseConflictError):
            competitor.acquire()
        await parent.release()
        competitor.acquire()
        await competitor.release()
        if kind == "native" and finish == "complete":
            assert requests[0]["model"] == "owned-fixture"
            assert any(message.get("role") == "tool" for message in requests[-1]["messages"])
    finally:
        (workspace / "release").touch()
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if helper_fd is not None:
            try:
                signal.pidfd_send_signal(helper_fd, signal.SIGKILL)
            except ProcessLookupError:
                pass
            os.close(helper_fd)
        if server is not None:
            server.shutdown()
            server.server_close()
