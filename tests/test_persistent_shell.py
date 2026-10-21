"""Feature 4: persistent stateful shell."""

import shutil
from pathlib import Path

import pytest

from garuda.tools.bash import BashTool
from garuda.tools.protocol import ToolContext
from garuda.workspace.local import LocalEnvironment

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")


async def test_persistent_shell_preserves_cwd_and_env(tmp_path: Path):
    from garuda.workspace.shell import PersistentShell

    (tmp_path / "sub").mkdir()
    shell = PersistentShell(cwd=str(tmp_path), env={"PATH": "/usr/bin:/bin"})
    try:
        r = await shell.run("cd sub && pwd")
        assert r.exit_code == 0 and r.stdout.strip().endswith("/sub")
        # cwd persisted from the previous command:
        r = await shell.run("pwd")
        assert r.stdout.strip().endswith("/sub")
        # env var persists across commands:
        await shell.run("export FOO=bar123")
        r = await shell.run("echo $FOO")
        assert r.stdout.strip() == "bar123"
    finally:
        await shell.close()


async def test_persistent_shell_exit_codes(tmp_path: Path):
    from garuda.workspace.shell import PersistentShell

    shell = PersistentShell(cwd=str(tmp_path))
    try:
        assert (await shell.run("true")).exit_code == 0
        assert (await shell.run("false")).exit_code == 1
        assert (await shell.run("echo hi")).stdout.strip() == "hi"
    finally:
        await shell.close()


async def test_persistent_shell_timeout_recovers(tmp_path: Path):
    from garuda.workspace.shell import PersistentShell

    shell = PersistentShell(cwd=str(tmp_path))
    try:
        r = await shell.run("sleep 30", timeout=1.0)
        assert r.exit_code == 124 and r.truncated
        # The shell is still usable afterward (interrupted or restarted).
        r = await shell.run("echo recovered")
        assert r.stdout.strip() == "recovered"
    finally:
        await shell.close()


async def test_bash_tool_persistent_mode_preserves_state(tmp_path: Path):
    (tmp_path / "d").mkdir()
    env = LocalEnvironment(workspace_root=tmp_path)
    ctx = ToolContext(session_id="s", persistent_shell=True)
    tool = BashTool()
    try:
        await tool.execute({"command": "cd d && export TOK=xyz"}, env, ctx)
        result = await tool.execute({"command": "pwd; echo $TOK"}, env, ctx)
        assert "/d" in result.content and "xyz" in result.content  # state persisted
    finally:
        await env.aclose()


async def test_bash_tool_stateless_by_default(tmp_path: Path):
    env = LocalEnvironment(workspace_root=tmp_path)
    ctx = ToolContext(session_id="s")  # persistent_shell defaults False
    tool = BashTool()
    await tool.execute({"command": "export TOK=shouldnotpersist"}, env, ctx)
    result = await tool.execute({"command": "echo [$TOK]"}, env, ctx)
    assert "[]" in result.content  # fresh subprocess each call -> no state carried


@pytest.mark.parametrize("name", ["a'$(touch injected)", "a'`touch injected`"])
async def test_per_call_cwd_is_literal_shell_data(tmp_path: Path, name):
    target = tmp_path / name
    target.mkdir()
    env = LocalEnvironment(workspace_root=tmp_path)
    try:
        result = await env.persistent_execute("pwd", cwd=name)
        assert not (tmp_path / "injected").exists(), "cwd executed a shell substitution"
        assert result.exit_code == 0
        assert Path(result.stdout.strip()) == target.resolve()
    finally:
        await env.aclose()


async def test_per_call_relative_cwd_uses_workspace_without_moving_the_shell(tmp_path: Path):
    (tmp_path / "session").mkdir()
    (tmp_path / "target").mkdir()
    env = LocalEnvironment(workspace_root=tmp_path)
    try:
        await env.persistent_execute("cd session")
        result = await env.persistent_execute("pwd", cwd="target")
        assert result.exit_code == 0
        assert result.stdout.strip() == (await env.execute("pwd", cwd="target")).stdout.strip()
        assert Path((await env.persistent_execute("pwd")).stdout.strip()) == (tmp_path / "session").resolve()
    finally:
        await env.aclose()


async def test_per_call_cwd_accepts_trailing_comments_and_preserves_state(tmp_path):
    (tmp_path / "sub").mkdir()
    env = LocalEnvironment(workspace_root=tmp_path)
    try:
        await env.persistent_execute("export KEEP=present")
        result = await env.persistent_execute("pwd # list files", cwd="sub", timeout=0.2)
        assert result.exit_code == 0
        assert Path(result.stdout.strip()) == (tmp_path / "sub").resolve()
        assert (await env.persistent_execute('echo "$KEEP"')).stdout.strip() == "present"
    finally:
        await env.aclose()


async def test_cancelled_shell_command_stops_before_next_call(tmp_path):
    import asyncio

    env = LocalEnvironment(workspace_root=tmp_path)
    running = asyncio.create_task(env.persistent_execute(
        "echo started > started; sleep 0.5; echo leaked; touch survived", timeout=5))
    try:
        async with asyncio.timeout(5):
            while not (tmp_path / "started").exists():
                await asyncio.sleep(0.01)
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
        result = await env.persistent_execute("echo recovered", timeout=2)
        assert result.stdout.strip() == "recovered"
        await asyncio.sleep(0.6)
        assert not (tmp_path / "survived").exists()
    finally:
        running.cancel()
        await asyncio.gather(running, return_exceptions=True)
        await env.aclose()


async def test_cancellation_during_shell_launch_reaps_the_created_process(tmp_path, monkeypatch):
    import asyncio

    from garuda.workspace.shell import PersistentShell

    real_spawn = asyncio.create_subprocess_exec
    launched = asyncio.Event()
    release = asyncio.Event()
    processes = []

    async def delayed_spawn(*args, **kwargs):
        proc = await real_spawn(*args, **kwargs)
        processes.append(proc)
        launched.set()
        await release.wait()
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", delayed_spawn)
    shell = PersistentShell(str(tmp_path))
    task = asyncio.create_task(shell.run("touch must-not-run"))
    try:
        await asyncio.wait_for(launched.wait(), timeout=5)
        task.cancel()
        await asyncio.sleep(0)  # deliver cancellation while launch is still pending
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=5)
        assert processes[0].returncode is not None
        assert not (tmp_path / "must-not-run").exists()
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await shell.close()
        for proc in processes:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()


async def test_unconfirmed_cancel_cleanup_preserves_cancellation_and_refuses_reuse(tmp_path, monkeypatch):
    import asyncio

    from garuda.workspace.shell import PersistentShell

    shell = PersistentShell(str(tmp_path))
    task = asyncio.create_task(shell.run("echo started > started; sleep 30"))
    real_wait = None
    try:
        async with asyncio.timeout(5):
            while not (tmp_path / "started").exists():
                await asyncio.sleep(0.01)
        proc = shell._proc
        real_wait = proc.wait

        async def cannot_confirm_exit():
            raise RuntimeError("exit confirmation failed")

        monkeypatch.setattr(proc, "wait", cannot_confirm_exit)
        task.cancel()
        with pytest.raises(asyncio.CancelledError) as cancelled:
            await task
        assert isinstance(cancelled.value.__cause__, RuntimeError)
        with pytest.raises(RuntimeError, match="cleanup is incomplete"):
            await shell.run("touch must-not-run")
        assert not (tmp_path / "must-not-run").exists()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        if real_wait is not None:
            monkeypatch.setattr(shell._proc, "wait", real_wait)
        await shell.close()
