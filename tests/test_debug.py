"""Optional debugging at the tool, permission and real adapter boundaries (#388)."""

import asyncio
import importlib.util
import json
import os
import shutil
import signal
import socket
import subprocess
from pathlib import Path

import pytest
import pytest_asyncio

from garuda.agents.setup import reap_owned_processes
from garuda.agents.spec_api import AgentSpec
from garuda.core.loop import DefaultAgent
from garuda.core.permissions import PermissionDecision, PermissionEngine
from garuda.model.protocol import ModelResponse
from garuda.model.script_model import ScriptModel
from garuda.runtime.debug_adapter import DebugError
from garuda.runtime.process_session import CleanupUnproved, session_groups
from garuda.tools import build_toolkit, default_tools, tools_for_names
from garuda.tools.protocol import ToolContext
from garuda.types import AgentConfig, Role, ToolCall
from garuda.workspace.docker import DockerEnvironment
from garuda.workspace.local import LocalEnvironment
from garuda.workspace.remote import RemoteEnvironment
from garuda.workspace.sandbox import SandboxEnvironment
from garuda.workspace.tmux import TmuxEnvironment

TOOLS = ["debug_launch", "debug_inspect", "debug_variables", "debug_control"]
FIXTURES = Path(__file__).parent / "fixtures"


async def call(name, env, ctx, **arguments):
    tools = tools_for_names([name])
    assert len(tools) == 1, f"optional debugger tool {name} is unavailable"
    result = await tools[0].execute(arguments, env, ctx)
    assert not result.is_error, result.content
    value = json.loads(result.content)
    assert value["schema"] == "garuda.debug" and value["version"] == 1
    assert value["session_id"] == ctx.session_id
    return value


async def test_debug_tools_require_explicit_selection_and_agent_options(tmp_path):
    default, _ = await build_toolkit(None)
    assert not set(TOOLS) & {t.name for t in default_tools() + default}
    for preset in ("all", "read-only"):
        ordinary = AgentSpec.from_dict({"tools": {"preset": preset}}, workspace=tmp_path)
        assert not set(TOOLS) & set(ordinary.profile().tools)
    spec = AgentSpec.from_dict({
        "extends": "garuda/build", "tools": {
            "add": TOOLS, "options": {"debug_launch": {"max_variables": 5, "max_depth": 2}}}},
        workspace=tmp_path)
    profile = spec.profile()
    selected, _ = await build_toolkit(profile.tools)
    assert set(TOOLS) <= {t.name for t in selected}
    assert profile.tool_options["debug_launch"]["max_variables"] == 5


async def test_readonly_denies_launch_before_any_debuggee_execution(tmp_path):
    (tmp_path / "main.py").write_text("from pathlib import Path\nPath('ran').touch()\n")
    result = await DefaultAgent().run(
        task="debug", env=LocalEnvironment(tmp_path), tools=tools_for_names(TOOLS),
        model=ScriptModel([
            ModelResponse(content=None, tool_calls=[ToolCall("launch", "debug_launch", {"program": "main.py"})]),
            ModelResponse(content="Stopped.", tool_calls=[])]),
        config=AgentConfig(max_turns=2, permission_mode="readonly", enable_verifier=False))
    messages = [m for m in result.messages if m.role == Role.TOOL]
    assert any(m.name == "debug_launch" and "Permission denied" in m.content for m in messages)
    assert not (tmp_path / "ran").exists()


async def test_readonly_refuses_variable_rendering_and_read_inspection_cannot_dispatch_it(tmp_path):
    permission = PermissionEngine(mode="readonly", tool_rules={"debug_variables": "allow"})
    allowed, reason = await permission.evaluate_tool_call(
        "debug_variables", {"debug_session_id": "unused", "operation": "variables", "handle": "unused"})
    assert not allowed and "Permission denied" in reason
    result = await tools_for_names(["debug_inspect"])[0].execute(
        {"debug_session_id": "unused", "operation": "variables"},
        LocalEnvironment(tmp_path), ToolContext("readonly"))
    assert result.is_error and "mutating debug_variables authority" in result.content


@pytest.mark.parametrize("engine, arguments, expected", [
    (PermissionEngine(mode="readonly", tool_rules={"debug_launch": "allow"}),
     {"program": "main.py"}, PermissionDecision.DENY),
    (PermissionEngine(tool_rules={"bash": "deny"}),
     {"program": "main.py"}, PermissionDecision.DENY),
    (PermissionEngine(bash_rules={"deny": ["main.py"]}),
     {"program": "main.py", "command": "ls"}, PermissionDecision.DENY),
    (PermissionEngine(path_rules={"deny": ["secret.py"]}),
     {"program": "secret.py"}, PermissionDecision.DENY),
    (PermissionEngine(bash_rules={"ask": ["main.py"]}),
     {"program": "main.py"}, PermissionDecision.ASK),
])
def test_debug_launch_honors_bash_and_path_policy(engine, arguments, expected):
    assert engine.decide("debug_launch", arguments)[0] == expected


@pytest.mark.parametrize("factory", [DockerEnvironment, RemoteEnvironment, SandboxEnvironment, TmuxEnvironment])
async def test_nonlocal_debugging_refuses_before_execution(factory, tmp_path):
    # Bypass constructors that would create/probe Docker/tmux: the real tool must
    # refuse based on backend, before touching any environment operation.
    env = object.__new__(factory)
    tool = tools_for_names(["debug_launch"])[0]
    result = await tool.execute({"program": "main.py"}, env, ToolContext("s"))
    assert result.is_error and "local workspaces only" in result.content


@pytest.mark.parametrize("outside", ["../outside.py", "link.py"])
async def test_launch_refuses_outside_workspace_even_through_symlink(tmp_path, outside):
    root = tmp_path / "workspace"
    root.mkdir()
    (tmp_path / "outside.py").write_text("raise SystemExit(99)")
    (root / "link.py").symlink_to(tmp_path / "outside.py")
    env = LocalEnvironment(root, confine_to_workspace=False)
    result = await tools_for_names(["debug_launch"])[0].execute(
        {"program": outside}, env, ToolContext("s"))
    assert result.is_error and "outside the workspace" in result.content


@pytest.mark.parametrize("extra", [{"request": "attach"}, {"evaluate": "1+1"},
                                  {"setVariable": {}}, {"raw": {}}, {"memory": 12}])
async def test_launch_refuses_unsupported_authority(extra, tmp_path):
    result = await tools_for_names(["debug_launch"])[0].execute(
        {"program": "missing.py", **extra}, LocalEnvironment(tmp_path), ToolContext("s"))
    assert result.is_error and "unsupported" in result.content


@pytest.mark.parametrize("status, table", [(1, ""), (0, ""), (0, "not a process table")])
async def test_missing_process_evidence_refuses_launch_without_claiming_cleanup(
    tmp_path, monkeypatch, status, table,
):
    (tmp_path / "main.py").write_text("from pathlib import Path\nPath('ran').touch()\n")
    monkeypatch.setattr("garuda.runtime.process_session.subprocess.run",
                        lambda *a, **kw: subprocess.CompletedProcess(a, status, stdout=table, stderr="denied"))
    env = LocalEnvironment(tmp_path)
    result = await tools_for_names(["debug_launch"])[0].execute(
        {"program": "main.py"}, env, ToolContext("s"))
    assert result.is_error and "cleanup unproved" in result.content
    assert not (tmp_path / "ran").exists()
    assert await reap_owned_processes("s", env) == []  # launch was refused before ownership


def require_debugpy():
    if importlib.util.find_spec("debugpy") is None:
        pytest.skip("debugpy is not installed")
    try:
        session_groups(os.getsid(0))
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
    except (CleanupUnproved, OSError) as exc:
        pytest.skip(f"real DAP integration unavailable: {exc}")


@pytest_asyncio.fixture
async def debug_env(tmp_path):
    require_debugpy()
    for name in ("debug_program.py", "debug_child.py"):
        shutil.copyfile(FIXTURES / name, tmp_path / name)
    env = LocalEnvironment(tmp_path)
    ctx = ToolContext("agent-one", tool_options={"debug_launch": {
        "max_variables": 5, "max_depth": 2, "max_chars": 2048, "output_chars": 1000}})
    try:
        yield env, ctx
    finally:
        await env.aclose()


async def launch(debug_env, program="debug_program.py", line=10):
    env, ctx = debug_env
    return await call("debug_launch", env, ctx, program=program, breakpoints=[line])


async def inspect(debug_env, session, operation, **kw):
    env, ctx = debug_env
    name = "debug_variables" if operation == "variables" else "debug_inspect"
    return await call(name, env, ctx, debug_session_id=session["debug_session_id"],
                      operation=operation, **kw)


async def control(debug_env, session, operation):
    env, ctx = debug_env
    return await call("debug_control", env, ctx, debug_session_id=session["debug_session_id"],
                      operation=operation)


async def test_real_breakpoints_stack_scopes_variables_and_bounds(debug_env):
    session = await launch(debug_env)
    assert session["state"] == "stopped"
    assert session["data"]["breakpoints"][0]["verified"] is True
    assert session["data"]["capabilities"]["supportsConfigurationDoneRequest"] is True
    frames = (await inspect(debug_env, session, "stackTrace"))["data"]["stackFrames"]
    assert frames[0]["line"] == 10
    scopes = (await inspect(debug_env, session, "scopes", handle=frames[0]["handle"]))["data"]["scopes"]
    local = next(s for s in scopes if s["name"] == "Locals")
    evidence = await inspect(debug_env, session, "variables", handle=local["handle"], count=999)
    assert len(json.dumps(evidence)) <= 2048
    variables = evidence["data"]["variables"]
    assert len(variables) <= 5
    assert any(v["name"] == "number" and v["value"] == "41" for v in variables)
    assert all(len(v["value"]) <= 256 for v in variables)
    nested = next(v for v in variables if v["name"] == "nested")
    children = await inspect(debug_env, session, "variables", handle=nested["handle"], count=999, depth=99)
    assert all("handle" not in v for v in children["data"]["variables"])
    assert len(children["data"]["variables"]) <= 5
    other = ToolContext("agent-two")
    result = await tools_for_names(["debug_inspect"])[0].execute(
        {"debug_session_id": session["debug_session_id"], "operation": "status"}, debug_env[0], other)
    assert result.is_error and "foreign debug session" in result.content


async def test_real_steps_in_out_next_and_stale_handles(debug_env):
    session = await launch(debug_env)
    old = (await inspect(debug_env, session, "stackTrace"))["data"]["stackFrames"][0]["handle"]
    stepped = await control(debug_env, session, "stepIn")
    assert stepped["state"] == "stopped" and stepped["stop_generation"] > session["stop_generation"]
    frame = (await inspect(debug_env, session, "stackTrace"))["data"]["stackFrames"][0]
    assert frame["name"] == "add_one" and frame["line"] == 2
    result = await tools_for_names(["debug_inspect"])[0].execute(
        {"debug_session_id": session["debug_session_id"], "operation": "scopes", "handle": old},
        *debug_env)
    assert result.is_error and "stale" in result.content
    await control(debug_env, session, "next")
    frame = (await inspect(debug_env, session, "stackTrace"))["data"]["stackFrames"][0]
    assert frame["line"] == 3
    await control(debug_env, session, "stepOut")
    frame = (await inspect(debug_env, session, "stackTrace"))["data"]["stackFrames"][0]
    # Back in the caller, still on the assignment the call was part of (as debugpy reports).
    assert frame["line"] == 10
    await control(debug_env, session, "continue")
    async with asyncio.timeout(10):
        while True:
            output = (await inspect(debug_env, session, "output"))["data"]
            if output["truncated"] and output["output"].endswith("\n"):
                break
            await asyncio.sleep(0.05)
    assert len(output["output"]) <= 1000 and "z" * 100 in output["output"]


@pytest.mark.parametrize("operation", ["attach", "evaluate", "setVariable", "readMemory",
                                        "writeMemory", "raw", "getters"])
async def test_live_session_refuses_unsupported_requests(debug_env, operation):
    session = await launch(debug_env)
    for name in ("debug_control", "debug_inspect"):
        result = await tools_for_names([name])[0].execute(
            {"debug_session_id": session["debug_session_id"], "operation": operation}, *debug_env)
        assert result.is_error and "unsupported" in result.content


@pytest.mark.parametrize("finish", ["disconnect", "terminate", "teardown", "cancel", "crash"])
async def test_real_lifecycle_kills_debuggee_and_its_child(debug_env, finish):
    env, ctx = debug_env
    session = await launch(debug_env, "debug_child.py", 8)
    child = int((Path(env.workspace_root) / "child.pid").read_text())
    os.kill(child, 0)
    owned = env.debug_service().get(ctx.session_id, session["debug_session_id"])
    supervisor_pid = owned.adapter.process.pid
    groups = session_groups(supervisor_pid)
    assert os.getpgid(child) in groups
    if finish in {"disconnect", "terminate"}:
        receipt = await control(debug_env, session, finish)
        assert receipt["cleanup"].startswith("verified:")
    elif finish == "teardown":
        assert await reap_owned_processes(ctx.session_id, env) == []
    elif finish == "cancel":
        await control(debug_env, session, "next")
        await control(debug_env, session, "next")
        task = asyncio.create_task(control(debug_env, session, "next"))
        await asyncio.sleep(0.3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        # Adapter is the supervisor's child in the same group; kill it without
        # killing the anchoring supervisor or the separate debuggee group.
        table = subprocess.check_output(["/bin/ps", "-Ao", "pid=,ppid="], text=True)
        adapter_pid = next(int(row.split()[0]) for row in table.splitlines()
                           if len(row.split()) == 2 and int(row.split()[1]) == supervisor_pid)
        os.kill(adapter_pid, signal.SIGKILL)
        async with asyncio.timeout(10):
            while owned.cleanup is None:
                await asyncio.sleep(0.05)
        assert owned.cleanup.startswith("verified:")
        result = await tools_for_names(["debug_inspect"])[0].execute(
            {"debug_session_id": session["debug_session_id"], "operation": "stackTrace"}, env, ctx)
        assert result.is_error and "disconnected" in result.content
    assert not session_groups(supervisor_pid)
    # Independently check the actual fixture child, rather than relying only on
    # the production ownership observer as the test's oracle.
    async with asyncio.timeout(3):
        while True:
            observed = subprocess.run(["/bin/ps", "-p", str(child), "-o", "stat="],
                                      capture_output=True, text=True, timeout=2)
            assert observed.returncode in {0, 1}, observed.stderr
            if not observed.stdout.strip() or observed.stdout.strip().startswith("Z"):
                break
            await asyncio.sleep(0.05)
    assert owned.adapter.process.returncode is not None


async def test_real_cleanup_uncertainty_is_retained_for_quarantine(tmp_path, monkeypatch):
    require_debugpy()
    shutil.copyfile(FIXTURES / "debug_child.py", tmp_path / "debug_child.py")
    env, ctx = LocalEnvironment(tmp_path), ToolContext("uncertain")
    try:
        session = await launch((env, ctx), "debug_child.py", 8)
        owned = env.debug_service().get(ctx.session_id, session["debug_session_id"])
        observations = 0

        def lose_verification(sid):
            nonlocal observations
            observations += 1
            if observations > 1:
                raise CleanupUnproved("cleanup unproved: process table became unavailable")
            return session_groups(sid)

        with monkeypatch.context() as patch:
            patch.setattr("garuda.runtime.process_session.session_groups", lose_verification)
            result = await tools_for_names(["debug_control"])[0].execute(
                {"debug_session_id": session["debug_session_id"], "operation": "disconnect"}, env, ctx)
        assert result.is_error and "cleanup unproved" in result.content
        survivors = await reap_owned_processes(ctx.session_id, env)
        assert survivors and all("cleanup unproved" in s for s in survivors)
        with pytest.raises(DebugError, match="cleanup unproved"):
            await env.aclose()
        # The kill happened, but loss of the contemporaneous closure proof must
        # still retain the failed receipt rather than retrospectively bless it.
        assert not session_groups(owned.adapter.process.pid)
    finally:
        try:
            await env.aclose()
        except DebugError:
            for remaining in env.debug_service().sessions.values():
                assert not session_groups(remaining.adapter.process.pid)
