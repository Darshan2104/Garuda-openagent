"""Cell refusal and runner contracts; live confinement is never simulated."""

import asyncio
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from garuda.context.manager import ContextManager
from garuda.core.action_memo import ActionMemo
from garuda.core.events import EventStore
from garuda.core.permissions import PermissionEngine
from garuda.core.side_effects import SideEffectLedger
from garuda.core.tool_runner import ToolRunner
from garuda.plugins.hooks import HookRegistry
from garuda.tools import build_toolkit, default_tools
from garuda.tools.code_cell import CodeCellTool
from garuda.tools.protocol import ToolContext
from garuda.types import ToolCall
from garuda.workspace.cells import WORKER, CellError, CellLimits, cell_process, exchange_cell
from garuda.workspace.docker import DockerWorkspace
from garuda.workspace.local import LocalEnvironment
from garuda.workspace.sandbox import SandboxEnvironment
from garuda.workspace.sandbox_policy import SandboxPolicy


def runner_for(env, tmp_path, *, options=None, rules=None):
    tools = [*default_tools(), CodeCellTool()]
    return ToolRunner(
        tool_map={tool.name: tool for tool in tools}, env=env,
        ctx=ToolContext(session_id="cells", tool_options={"code_cell": options or {}}),
        context=ContextManager(model=None), permissions=PermissionEngine(
            workspace_root=env.workspace_root, tool_rules=rules or {},
        ), hooks=HookRegistry(), events=EventStore(
            session_id="cells", persist_path=tmp_path / "events.jsonl",
        ), ledger=SideEffectLedger(), memo=ActionMemo(),
    )


async def run_cell(runner, code, inputs=None):
    call = ToolCall(id="outer", name="code_cell", arguments={"code": code, "inputs": inputs or {}})
    screened, denied = await runner.screen(call, 1)
    assert denied is None
    return await runner.run_one(screened, 1)


async def test_code_cells_require_explicit_selection():
    implicit, _ = await build_toolkit(None)
    explicit, _ = await build_toolkit(["code_cell"])
    assert "code_cell" not in {tool.name for tool in implicit + default_tools()}
    assert [tool.name for tool in explicit] == ["code_cell"]
    # The broad preset remains backwards compatible; tools.add is the opt-in.
    from garuda.agents.spec_api import AgentSpec

    profile = AgentSpec.from_dict({"version": 1, "name": "cell", "tools": {
        "preset": "all", "add": ["code_cell"], "options": {"code_cell": {"timeout": 2}},
    }}).profile()
    assert "code_cell" in profile.tools
    broad = AgentSpec.from_dict({"version": 1, "name": "broad", "tools": {"preset": "all"}}).profile()
    assert "code_cell" not in broad.tools


@pytest.mark.parametrize("backend", ["local", None, "seatbelt"])
async def test_refuses_without_full_confinement(tmp_path, monkeypatch, backend):
    if backend == "local":
        env = LocalEnvironment(tmp_path)
    else:
        monkeypatch.setattr("garuda.workspace.sandbox.detect_sandbox_backend", lambda: backend)
        env = SandboxEnvironment(tmp_path, SandboxPolicy(require_sandbox=False))
    runner = runner_for(env, tmp_path)
    result = await run_cell(runner, "open('escaped', 'w').write('bad')")
    assert result.is_error and "unsupported" in result.content
    assert not (tmp_path / "escaped").exists()
    records = [json.loads(line) for line in (tmp_path / "events.jsonl").read_text().splitlines()]
    call, receipt = records
    assert call["payload"]["cell_id"] == receipt["payload"]["cell_id"]
    assert receipt["payload"]["nested_index"] == 0
    assert receipt["payload"]["is_error"]


async def test_readonly_cell_is_denied_before_execution(tmp_path):
    runner = runner_for(LocalEnvironment(tmp_path), tmp_path)
    runner.permissions = PermissionEngine(mode="readonly")
    call, denial = await runner.screen(ToolCall("readonly", "code_cell", {"code": "result=1"}), 1)
    assert call is None and "Permission denied" in denial.content


async def test_standalone_tool_requires_the_ordinary_runner(tmp_path):
    env = SandboxEnvironment(tmp_path, SandboxPolicy(require_sandbox=False))
    result = await CodeCellTool().execute(
        {"code": "open('escaped', 'w').write('bad')"}, env, ToolContext(session_id="standalone"),
    )
    assert result.is_error and "ToolRunner bridge is unavailable" in result.content
    assert not (tmp_path / "escaped").exists()


@pytest.fixture
async def confined_env(tmp_path):
    sandbox = SandboxEnvironment(tmp_path, SandboxPolicy(require_sandbox=False))
    if sandbox.backend == "bwrap" and str(Path(sys.executable).resolve()).startswith(("/usr/", "/bin/")):
        probe = await sandbox.execute("true", timeout=5)
        if probe.exit_code == 0:
            yield sandbox
            return
    docker = shutil.which("docker")
    if not docker:
        pytest.skip("No full OS confinement and Docker executable unavailable")
    probe = subprocess.run([docker, "info"], capture_output=True, timeout=15)
    if probe.returncode:
        pytest.skip("Docker daemon unavailable: " + probe.stderr.decode(errors="replace").strip()[:240])
    image = subprocess.run([docker, "image", "inspect", "python:3.12-slim"], capture_output=True, timeout=15)
    if image.returncode:
        pytest.skip("Docker cell integration requires preinstalled python:3.12-slim (no automatic pulls)")
    workspace = DockerWorkspace(tmp_path, image="python:3.12-slim")
    await workspace.start()
    try:
        yield workspace.get_environment()
    finally:
        await workspace.stop()


async def test_bridge_transport_screens_hooks_accounts_and_records(tmp_path):
    env = LocalEnvironment(tmp_path)
    runner = runner_for(env, tmp_path, rules={"write_file": "deny"})
    before, after = [], []

    async def before_hook(call, context):
        before.append((call.name, context.get("nested_index")))
        return call

    async def after_hook(call, result, context):
        after.append((call.name, context.get("nested_index")))
        return result

    runner.hooks.register_before_tool(before_hook)
    runner.hooks.register_after_tool(after_hook)
    direct, denied = await runner.screen(ToolCall("direct", "write_file", {"path": "bad", "content": "x"}), 1)
    assert direct is None
    await env.write_file("data.txt", "alpha\nbeta\n")

    async def transport(code):
        async def callback(name, arguments, index):
            return await runner._run_cell_tool(name, arguments, index, 1, "transport-cell")

        # Exercise the actual host bridge through a real stdio worker; this is
        # transport/dispatch proof and does not admit the host as confinement.
        async with cell_process([sys.executable, "-I", "-u", "-c", WORKER]) as process:
            return await exchange_cell(process, code, {}, CellLimits(), callback)

    result = await transport('''
denied = tool("write_file", {"path": "bad", "content": "x"})
read = tool("read_file", {"path": "data.txt"})
result = {"denied": denied, "lines": len(read["content"].splitlines())}
''')
    assert "error" not in result, result
    value = result["result"]
    assert value["denied"] == {"content": denied.content, "is_error": True}
    assert value["lines"] == 2
    assert ("read_file", 2) in before and ("read_file", 2) in after
    assert not any(name == "write_file" for name, _ in before)
    messages = runner.context.get_messages()
    assert messages == []
    records = [json.loads(line) for line in (tmp_path / "events.jsonl").read_text().splitlines()]
    nested = [r for r in records if r["payload"].get("nested_index", 0) > 0]
    assert {r["payload"]["cell_id"] for r in nested} == {"transport-cell"}
    assert {r["payload"]["nested_index"] for r in nested} == {1, 2}
    assert any(r["type"] == "permission_ask" and r["payload"]["approved"] is False for r in nested)
    assert sum(r["type"] == "tool_result" for r in nested) == 2
    # A later permitted mutation must also pass through the normal effects owner.
    runner.permissions = PermissionEngine(workspace_root=env.workspace_root)
    result = await transport('result=tool("write_file", {"path":"ok", "content":"saved"})')
    assert not result["result"]["is_error"]
    assert await env.read_file("ok") == "saved"
    assert "ok" in runner.ledger.files_written
    # Lazy target resolution and hook rewrites must re-enter screening too.
    from garuda.tools.discovery import UseToolTool

    runner.tool_map["use_tool"] = UseToolTool({"write_file": runner.tool_map["write_file"]})
    runner.permissions = PermissionEngine(tool_rules={"write_file": "deny"})
    value = await transport('result=tool("use_tool", {"name":"write_file", "arguments":{"path":"bad", "content":"x"}})')
    assert value["result"] == {"content": denied.content, "is_error": True}

    async def rewrite(call, context):
        if call.name == "read_file":
            return ToolCall(call.id, "write_file", {"path": "bad", "content": "x"})
        return call

    runner.hooks.register_before_tool(rewrite)
    value = await transport('result=tool("read_file", {"path":"data.txt"})')
    assert value["result"] == {"content": denied.content, "is_error": True}
    assert not (tmp_path / "bad").exists()


async def test_confined_tool_bridge_roundtrip(confined_env, tmp_path):
    runner = runner_for(confined_env, tmp_path, rules={"write_file": "deny"})
    result = await run_cell(runner, 'result=tool("write_file", {"path":"bad", "content":"x"})')
    assert not result.is_error, result.content
    assert json.loads(result.content)["result"] == {
        "content": "Permission denied for tool: write_file", "is_error": True,
    }
    receipts = [r["payload"] for r in runner.events.get_all() if r["type"] == "tool_result"]
    assert [r["nested_index"] for r in receipts] == [1, 0]
    assert receipts[0]["cell_id"] == receipts[1]["cell_id"]
    assert [m.tool_call_id for m in runner.context.get_messages()] == ["outer"]


@pytest.mark.parametrize(("code", "options", "reason"), [
    ("x" * 33_000, {}, "code/input byte limit"),
    ("while True: pass", {"timeout": 0.2}, "wall-time"),
    ("while True: print('x'*1024)", {"output_bytes": 1024}, "byte limit"),
    ("for _ in range(3): tool('ls', {})", {"nested_calls": 2}, "nested-call"),
    ("import os; os._exit(1)", {}, "without a result"),
])
async def test_confined_cell_bounds_and_death(confined_env, tmp_path, code, options, reason):
    result = await run_cell(runner_for(confined_env, tmp_path, options=options), code)
    assert result.is_error and reason in result.content, result.content


async def test_confined_direct_apis_cannot_read_host_or_connect(confined_env, tmp_path):
    canary = tmp_path / "host-only-secret"
    canary.write_text("host secret")
    runner = runner_for(confined_env, tmp_path)
    result = await run_cell(runner, '''
import socket
try:
    open(inputs["canary"]).read()
except OSError:
    read_blocked = True
else:
    read_blocked = False
try:
    socket.create_connection(("1.1.1.1", 53), timeout=0.2)
except OSError:
    network_blocked = True
else:
    network_blocked = False
result = [read_blocked, network_blocked]
''', {"canary": str(canary)})
    assert not result.is_error, result.content
    assert json.loads(result.content)["result"] == [True, True]


async def test_confined_cancellation_records_nested_and_outer(confined_env, tmp_path):
    runner = runner_for(confined_env, tmp_path)
    reached = asyncio.Event()

    async def park(call, context):
        if call.name == "read_file":
            reached.set()
            await asyncio.Event().wait()
        return call

    runner.hooks.register_before_tool(park)
    task = asyncio.create_task(run_cell(runner, 'tool("read_file", {"path":"data"})'))
    await asyncio.wait_for(reached.wait(), 10)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    receipts = [r["payload"] for r in runner.events.get_all() if r["type"] == "tool_result"]
    assert [r["nested_index"] for r in receipts] == [1, 0]
    assert all("cancelled" in receipt["content"] for receipt in receipts)


@pytest.mark.parametrize(("code", "limits", "reason"), [
    ("while True: pass", CellLimits(timeout=0.1), "wall-time"),
    ("while True: print('é'*1024)", CellLimits(output_bytes=1024), "byte limit"),
    ("import os\nwhile True: os.write(2, b'x'*1024)", CellLimits(output_bytes=1024), "byte limit"),
    ("for _ in range(3): tool('ls', {})", CellLimits(nested_calls=2), "nested-call"),
    ("import os; os.write(1,b'x'*70000)", CellLimits(), "frame byte limit"),
    ("import os; os._exit(1)", CellLimits(), "without a result"),
])
async def test_transport_bounds_real_worker(code, limits, reason):
    # This checks the transport/lifetime owner, not a claim of host confinement.
    calls = []

    async def callback(name, arguments, index):
        calls.append(index)
        return {"content": "ok", "is_error": False}

    async with cell_process([sys.executable, "-I", "-u", "-c", WORKER]) as process:
        with pytest.raises(CellError, match=reason):
            await exchange_cell(process, code, {}, limits, callback)
    assert process.returncode is not None
    assert len(calls) <= limits.nested_calls
    if reason == "nested-call":
        assert calls == [1, 2]


async def test_transport_cancellation_reaps_process_tree(tmp_path):
    reached = asyncio.Event()
    marker = tmp_path / "orphan-ran"
    runner = runner_for(LocalEnvironment(tmp_path), tmp_path)

    async def park(call, context):
        reached.set()
        await asyncio.Event().wait()

    runner.hooks.register_before_tool(park)

    async def callback(name, arguments, index):
        return await runner._run_cell_tool(name, arguments, index, 1, "cancelled-cell")

    async def run():
        async with cell_process([sys.executable, "-I", "-u", "-c", WORKER]) as process:
            code = '''
import subprocess, sys
child = subprocess.Popen([sys.executable, "-c", inputs["child"]])
tool("read_file", {"path": "unused"})
'''
            child = f"import time; from pathlib import Path; time.sleep(0.4); Path({str(marker)!r}).touch()"
            await exchange_cell(process, code, {"child": child}, CellLimits(), callback)

    task = asyncio.create_task(run())
    await asyncio.wait_for(reached.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 5)
    await asyncio.sleep(0.6)
    assert not marker.exists()
    receipts = [r["payload"] for r in runner.events.get_all() if r["type"] == "tool_result"]
    assert len(receipts) == 1
    assert receipts[0]["cell_id"] == "cancelled-cell" and receipts[0]["nested_index"] == 1
    assert "cancelled before dispatch" in receipts[0]["content"]


class HostTransportEnvironment(LocalEnvironment):
    """Runs the real worker on the host to time the bridge; not a confinement claim."""

    def open_code_cell(self):
        return cell_process([sys.executable, "-I", "-u", "-c", WORKER])


async def test_approval_wait_is_not_charged_to_the_cell_budget(tmp_path):
    asked = []

    async def slow_human(action):
        asked.append(action)
        await asyncio.sleep(0.8)
        return True

    env = HostTransportEnvironment(tmp_path)
    await env.write_file("data.txt", "alpha\nbeta\n")
    runner = runner_for(env, tmp_path, options={"timeout": 0.5})
    runner.permissions = PermissionEngine(
        workspace_root=env.workspace_root, tool_rules={"read_file": "ask"}, approval_handler=slow_human,
    )
    result = await run_cell(runner, '''
first = tool("read_file", {"path": "data.txt"})
second = tool("read_file", {"path": "data.txt"})
result = [first["is_error"], "beta" in second["content"]]
''')
    assert not result.is_error, result.content
    assert json.loads(result.content)["result"] == [False, True]
    assert len(asked) == 2
    receipts = [r["payload"] for r in runner.events.get_all() if r["type"] == "tool_result"]
    assert [r["nested_index"] for r in receipts] == [1, 2, 0]
    assert not any("cancelled" in r["content"] for r in receipts)


async def test_cell_own_compute_stays_bounded_around_nested_calls(tmp_path):
    env = HostTransportEnvironment(tmp_path)
    runner = runner_for(env, tmp_path, options={"timeout": 0.5})

    async def slow_hook(call, context):
        await asyncio.sleep(0.8)
        return call

    runner.hooks.register_before_tool(slow_hook)
    result = await run_cell(runner, 'tool("ls", {})\nwhile True: pass')
    assert result.is_error and "wall-time" in result.content, result.content
    receipts = [r["payload"] for r in runner.events.get_all() if r["type"] == "tool_result"]
    assert [r["nested_index"] for r in receipts] == [1, 0]
    assert not receipts[0]["is_error"]


async def test_transport_budget_excludes_slow_callbacks():
    async def callback(name, arguments, index):
        await asyncio.sleep(0.4)
        return {"content": "ok", "is_error": False}

    async with cell_process([sys.executable, "-I", "-u", "-c", WORKER]) as process:
        result = await exchange_cell(
            process, "result=[tool('ls', {})['content'] for _ in range(2)]", {},
            CellLimits(timeout=0.3), callback,
        )
    assert result["result"] == ["ok", "ok"]


async def test_worker_death_settles_an_outstanding_callback():
    settled = asyncio.Event()

    async def callback(name, arguments, index):
        try:
            await asyncio.Event().wait()
        finally:
            settled.set()

    code = """
import subprocess, sys
subprocess.Popen([sys.executable, "-c", "import os,time; time.sleep(0.2); os.kill(os.getppid(),9)"])
tool("ls", {})
"""
    async with cell_process([sys.executable, "-I", "-u", "-c", WORKER]) as process:
        with pytest.raises(CellError, match="exited during tool dispatch"):
            await asyncio.wait_for(exchange_cell(process, code, {}, CellLimits(), callback), 3)
    assert settled.is_set()
