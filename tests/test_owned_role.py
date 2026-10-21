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
from garuda.flows.launch import launch_step
from garuda.interfaces.run_guard import WorkspaceLeaseGuard
from garuda.runtime import owned_role
from garuda.runtime.capacity import CapacityStore, CapacityUnavailable
from garuda.runtime.owned_role import execute_owned_role, prepare_owned_step
from garuda.runtime.role_control import RoleControl
from garuda.runtime.roles import RolePlan, RoleRefused
from garuda.workspace.lease import LeaseConflictError, LeaseError, LeaseStore

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
    monkeypatch.setenv("OPENAI_API_KEY", "local-protocol-fixture")
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
        "max_turns: 3\n")
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
    admitted = []
    prepare = owned_role.prepare_owned_step

    def observe_preparation(launch):
        request = prepare(launch, timeout=30)
        admitted.append(request)
        if kind == "native":
            monkeypatch.setenv("GARUDA_REASONING_MODEL", "openai/wrong-model")
        return request

    monkeypatch.setattr(owned_role, "prepare_owned_step", observe_preparation)
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
        capability = parent.delegate(f"{parent.session_id}:build:1")
        launch = StepLaunch(parent.session_id, "build", "builder", role, "run the fixture",
                            str(workspace), capability, 1, False, store, require_owned=True)
        task = asyncio.create_task(launch_step(launch))
        await appears(workspace / "writes", task)
        request = admitted[0]
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
            assert result.closure["parent_id"] == parent.session_id
            assert result.closure["step_id"] == "build" and result.closure["attempt"] == 1
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


@linux
@pytest.mark.parametrize("change", ["during-parse", "ceiling", "absent-source", "project-memory"])
async def test_changed_admitted_source_refuses_before_any_role_tool_starts(case, monkeypatch, change):
    from garuda.agents import setup
    from garuda.core.acceptance_work import AcceptanceWorkError

    workspace, parent, _leases, store = case
    settings = Path(os.environ["GARUDA_GLOBAL_SETTINGS"])
    server, requests = install_native(workspace, settings)
    try:
        role = role_agent.bind(RolePlan("builder", "native", "native", permissions="auto", profile="owned"), str(workspace))
        capability = parent.delegate(f"{parent.session_id}:build:1")
        launch = StepLaunch(parent.session_id, "build", "builder", role, "refuse changed source",
                            str(workspace), capability, 1, False, store)
        if change == "during-parse":
            original = setup.static_agent_config

            def changed_after_read(*args, **kwargs):
                config = original(*args, **kwargs)
                (workspace / ".agent" / "agents" / "owned.yaml").write_text("name: changed\n")
                return config

            monkeypatch.setattr(setup, "static_agent_config", changed_after_read)
        request = prepare_owned_step(launch, timeout=5)
        if change == "ceiling":
            settings.write_text(settings.read_text() + "project_permission_ceiling: readonly\n")
        elif change == "absent-source":
            (settings.parent / "mcp.json").write_text('{"mcpServers": {}}')
        elif change == "project-memory":
            (workspace / "AGENTS.md").write_text("changed instructions after admission")
        with pytest.raises(AcceptanceWorkError, match="SourceChanged"):
            await execute_owned_role(request, capability)
        assert not (workspace / "helper").exists()
        assert not requests
        parent._check_authority()
        assert not parent._quarantined
    finally:
        (workspace / "release").touch()
        server.shutdown()
        server.server_close()


@linux
@pytest.mark.parametrize("permissions,no_edits", [("auto", True), ("readonly", False)])
async def test_owned_acp_readonly_refuses_before_host_cli_can_write(case, permissions, no_edits):
    workspace, parent, _leases, store = case
    settings = Path(os.environ["GARUDA_GLOBAL_SETTINGS"])
    shim = workspace.parent / "write_anyway_acp.py"
    shim.write_text(
        f"import sys; sys.path.insert(0, {str(Path(__file__).resolve().parents[1])!r})\n"
        "from garuda.acp.fake_agent import main\n"
        "raise SystemExit(main(['--profile', 'write-anyway']))\n")
    settings.write_text(yaml.safe_dump({"runtimes": [{"runtime_id": "owned-acp", "kind": "acp",
        "command": [sys.executable, str(shim)],
        "version": "1"}]}))
    role = RolePlan("reviewer", "owned-acp", "acp", permissions=permissions)
    launch = StepLaunch(parent.session_id, "review", "reviewer", role, "review this workspace",
                        str(workspace), parent.delegate(f"{parent.session_id}:review:1"),
                        1, no_edits, store, require_owned=True)
    with pytest.raises(RoleRefused, match="workspace.readonly_unenforced"):
        await launch_step(launch)
    assert not (workspace / "sneaky.txt").exists()
    assert store.list_sessions() == []


@linux
@pytest.mark.skipif(os.getenv("GARUDA_LIVE_OWNED_DOCKER") != "1", reason="opt-in owned Docker lifecycle")
@pytest.mark.parametrize("ending", ["normal", "cancel", "timeout", "cleanup-unavailable", "foreign-name",
                                    "record-changed", "identity-changed", "missing-image"])
async def test_owned_readonly_acp_proves_daemon_container_closure(case, ending, monkeypatch):
    workspace, parent, _leases, store = case
    workspace.chmod(0o755)
    # Docker Desktop maps bind-mount ownership across hosts. Trust only this
    # fixture path in a private config, never the user's global Git config.
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(workspace.parent / "gitconfig"))
    subprocess.run(["git", "config", "--global", "--add", "safe.directory", str(workspace)],
                   cwd=workspace, check=True)
    settings = Path(os.environ["GARUDA_GLOBAL_SETTINGS"])
    command = ["python", "/opt/garuda/garuda/acp/fake_agent.py", "--profile",
               "probe-writes" if ending in {"normal", "cleanup-unavailable", "record-changed", "identity-changed"}
               else "slow"]
    settings.write_text(yaml.safe_dump({"runtimes": [{
        "runtime_id": "owned-acp", "kind": "acp", "command": command, "version": "1"}],
        "capacity": {"owned-acp": 1}}))
    role = RolePlan("reviewer", "owned-acp", "acp", permissions="readonly", harness={
        "confinement": {"image": ("garuda-owned-acp-fixture-missing:local" if ending == "missing-image"
                                   else "garuda-owned-acp-fixture:local")}})
    launch = StepLaunch(parent.session_id, "review", "reviewer", role, "review this workspace",
                        str(workspace), parent.delegate(f"{parent.session_id}:review:1"),
                        1, True, store, require_owned=True)
    request = prepare_owned_step(launch, timeout=4 if ending == "timeout" else 30)
    prefix = f"garuda-role-{request.session_id}"
    if ending == "cleanup-unavailable":
        from garuda.runtime.process_supervision import CleanupUncertain
        from garuda.workspace.owned_confinement import DockerRoleOwner

        original_remove = DockerRoleOwner._remove

        async def unavailable(owner, name):
            if name.endswith("-run"):
                raise CleanupUncertain("Docker removal is unavailable")
            await original_remove(owner, name)

        monkeypatch.setattr(DockerRoleOwner, "_remove", unavailable)
    if ending == "foreign-name":
        subprocess.run(["docker", "create", "--name", f"{prefix}-probe", "--label",
                        "garuda.role-owner=foreign", "--pull", "never",
                        "garuda-owned-acp-fixture:local", "sleep", "60"], check=True, capture_output=True)
    if ending in {"record-changed", "identity-changed"}:
        from garuda.workspace.owned_confinement import DockerRoleOwner

        original_create = DockerRoleOwner._create

        async def changed(owner, kind, command):
            identity = await original_create(owner, kind, command)
            if kind == "run":
                record = owner.records / ("intent.json" if ending == "record-changed" else "run.json")
                data = json.loads(record.read_text())
                if ending == "record-changed":
                    data["nonce"] = "changed-after-container-creation"
                else:
                    data["id"] = "0" * 64
                record.write_text(json.dumps(data))
            return identity

        monkeypatch.setattr(DockerRoleOwner, "_create", changed)

    def containers(running=False):
        result = subprocess.run(["docker", "container", "ls", "--all", "--quiet", "--no-trunc",
                                 "--filter", f"name={prefix}" + ("-run" if running else ""),
                                 *(["--filter", "status=running"] if running else [])],
                                capture_output=True, check=True)
        return result.stdout.split()

    task = asyncio.create_task(execute_owned_role(request, launch.capability))
    try:
        if ending == "cancel":
            deadline = asyncio.get_running_loop().time() + 20
            while not (running := await asyncio.to_thread(containers, True)):
                if task.done():
                    await task
                    pytest.fail("role ended before creating its owned container")
                assert asyncio.get_running_loop().time() < deadline
                await asyncio.sleep(.02)
            identity = json.loads((store.root / "owned-docker" / request.session_id / "run.json").read_text())
            assert identity["id"].encode() in running
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        elif ending == "missing-image":
            from garuda.workspace.confined_acp import ConfinementRefused

            with pytest.raises(ConfinementRefused):
                await task
            assert not parent._quarantined
            capacity = CapacityStore()
            reservation = capacity.reserve("owned-acp", "refusal-released-capacity", 1)
            capacity.release(reservation)
        elif ending in {"timeout", "cleanup-unavailable", "foreign-name", "record-changed", "identity-changed"}:
            from garuda.runtime.process_supervision import CleanupUncertain

            with pytest.raises(CleanupUncertain):
                await task
            assert parent._quarantined
            with pytest.raises(CapacityUnavailable):
                CapacityStore().reserve("owned-acp", "must-stay-blocked", 1)
        else:
            result = await task
            assert result.success and "wrote=scratch " in result.output
            assert "refused=workspace,git" in result.output
            assert result.closure["confinement"] == {
                "backend": "docker", "absent": [f"{prefix}-probe", f"{prefix}-run"]}
            assert not parent._quarantined
        remaining = await asyncio.to_thread(containers)
        assert bool(remaining) is (ending in {"cleanup-unavailable", "foreign-name"})
        records = store.root / "owned-docker" / request.session_id
        intent = json.loads((records / "intent.json").read_text())
        assert intent["parent_id"] == parent.session_id
        assert intent["names"] == {"probe": f"{prefix}-probe", "run": f"{prefix}-run"}
        assert (records / "closure.json").exists() is (
            not remaining and ending not in {"record-changed", "identity-changed"})
        assert not (workspace / ".garuda-probe").exists()
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        # Test hygiene only, after assertions: exact fixture IDs, never names
        # or containers outside this request's unique prefix.
        for container in await asyncio.to_thread(containers):
            subprocess.run(["docker", "rm", "--force", container.decode()], check=True,
                           capture_output=True)


@linux
@pytest.mark.parametrize("policy", ["ask", "no-edits"])
async def test_owned_native_preserves_actual_ask_broker_and_no_edits(case, monkeypatch, policy):
    workspace, parent, _leases, store = case
    settings = Path(os.environ["GARUDA_GLOBAL_SETTINGS"])
    server, requests = install_native(workspace, settings)
    monkeypatch.setenv("OPENAI_API_KEY", "local-protocol-fixture")
    profile = workspace / ".agent" / "agents" / "owned.yaml"
    profile.write_text(profile.read_text() + "tool_rules: {bash: ask}\n")
    approvals = []

    async def approve(action):
        approvals.append(action)
        return True

    task = None
    try:
        role = role_agent.bind(RolePlan("builder", "native", "native", permissions="auto", profile="owned"), str(workspace))
        capability = parent.delegate(f"{parent.session_id}:build:1")
        launch = StepLaunch(parent.session_id, "build", "builder", role, "exercise permissions",
                            str(workspace), capability, 1, policy == "no-edits", store)
        request = prepare_owned_step(launch, timeout=20)
        if policy == "no-edits":
            (workspace / "release").touch()
        task = asyncio.create_task(execute_owned_role(request, capability, approval=approve))
        if policy == "ask":
            await appears(workspace / "writes", task)
            (workspace / "release").touch()
        result = await task
        assert result.closure["process"]["cleanup"]["echild"] is True
        assert any(message["role"] == "tool" for request in requests for message in request["messages"])
        if policy == "ask":
            assert len(approvals) == 1
            assert "bash" in approvals[0] or "python" in approvals[0]
            assert not Path(f"/proc/{int((workspace / 'helper').read_text())}").exists()
        else:
            assert approvals == []
            assert not (workspace / "helper").exists()
            assert not (workspace / "writes").exists()
    finally:
        (workspace / "release").touch()
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        server.shutdown()
        server.server_close()


@linux
@pytest.mark.parametrize("fault", ["supervisor-loss", "result-channel-loss", "source-helper-loss"])
async def test_owned_role_uncertainty_quarantines_the_original_owner_and_capacity(
        case, monkeypatch, fault):
    from garuda.runtime.process_supervision import CleanupUncertain

    workspace, parent, leases, store = case
    settings = Path(os.environ["GARUDA_GLOBAL_SETTINGS"])
    server, _requests = install_native(workspace, settings)
    monkeypatch.setenv("OPENAI_API_KEY", "local-protocol-fixture")
    captured = []
    start = RoleControl.start
    dispatch = RoleControl._dispatch

    async def observe(control, process):
        endpoint = await start(control, process)
        captured.append(process)
        return endpoint

    async def lose_result(control, operation, value):
        if operation == "result":
            raise OSError("injected result channel failure")
        return await dispatch(control, operation, value)

    monkeypatch.setattr(RoleControl, "start", observe)
    if fault == "result-channel-loss":
        monkeypatch.setattr(RoleControl, "_dispatch", lose_result)
    elif fault == "source-helper-loss":
        from garuda.core import acceptance_work

        owned_call = acceptance_work.owned_call
        source_checks = 0

        async def lose_source_evidence(operation, *args, **kwargs):
            nonlocal source_checks
            result = await owned_call(operation, *args, **kwargs)
            if operation == "admitted-sources":
                source_checks += 1
                if source_checks == 2:
                    raise CleanupUncertain("post-run source helper lost its closure evidence")
            return result

        monkeypatch.setattr(acceptance_work, "owned_call", lose_source_evidence)
    task = None
    handles = []
    try:
        role = role_agent.bind(RolePlan("builder", "native", "native", permissions="auto", profile="owned"), str(workspace))
        capability = parent.delegate(f"{parent.session_id}:build:1")
        request = prepare_owned_step(StepLaunch(parent.session_id, "build", "builder", role, "exercise loss",
                                     str(workspace), capability, 1, False, store), timeout=20)
        task = asyncio.create_task(execute_owned_role(request, capability))
        await appears(workspace / "writes", task)
        process = captured[0]
        for pid in (process.command["pid"], int((workspace / "helper").read_text())):
            handles.append(os.pidfd_open(pid))
        if fault == "supervisor-loss":
            supervisor = os.pidfd_open(process.proc.pid)
            try:
                signal.pidfd_send_signal(supervisor, signal.SIGKILL)
            finally:
                os.close(supervisor)
        else:
            (workspace / "release").touch()
        with pytest.raises(CleanupUncertain):
            await asyncio.wait_for(task, 15)
        assert parent._quarantined
        with pytest.raises(CapacityUnavailable):
            CapacityStore().reserve("native", "real-competitor", 1)
        competitor = WorkspaceLeaseGuard(str(workspace), str(uuid.uuid4()), lease_store=leases)
        with pytest.raises(LeaseConflictError):
            competitor.acquire()
        await parent.release()
        with pytest.raises(LeaseConflictError):
            competitor.acquire()
    finally:
        (workspace / "release").touch()
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        for handle in handles:
            try:
                signal.pidfd_send_signal(handle, signal.SIGKILL)
            except ProcessLookupError:
                pass
            os.close(handle)
        server.shutdown()
        server.server_close()


@linux
@pytest.mark.parametrize("caller", ["copied-record", "revoked", "wrong-step", "read-only-parent", "reissued-parent"])
async def test_owned_role_refuses_foreign_or_insufficient_authority_before_execution(case, caller):
    workspace, parent, leases, store = case
    settings = Path(os.environ["GARUDA_GLOBAL_SETTINGS"])
    server, requests = install_native(workspace, settings)
    readonly = None
    try:
        role = role_agent.bind(RolePlan("builder", "native", "native", permissions="auto", profile="owned"), str(workspace))
        capability = parent.delegate(f"{parent.session_id}:build:1")
        request = prepare_owned_step(StepLaunch(parent.session_id, "build", "builder", role, "refuse authority",
                                     str(workspace), capability, 1, False, store), timeout=5)
        if caller == "copied-record":
            capability = {"workspace": str(workspace), "session_id": capability.session_id,
                          "owner": parent.snapshot_ownership()}
        elif caller == "revoked":
            parent.revoke(capability)
        elif caller == "wrong-step":
            capability = parent.delegate(f"{parent.session_id}:different:1")
        elif caller == "reissued-parent":
            await parent.release()
            parent.acquire()
            parent.start_heartbeat()
        else:
            # A genuine read-only admission must not lend mutation authority.
            readonly = WorkspaceLeaseGuard(str(workspace), str(uuid.uuid4()), lease_store=leases,
                                           mode="read-only")
            readonly.acquire()
            capability = readonly.delegate("read-only-child")
        with pytest.raises((LeaseError, ValueError)):
            await execute_owned_role(request, capability)
        assert not (workspace / "helper").exists()
        assert requests == []
    finally:
        if readonly is not None:
            await readonly.release()
        server.shutdown()
        server.server_close()
