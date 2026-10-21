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
