"""Real tmux pane semantics on a private test server."""

import shutil
import subprocess
import tempfile

import pytest

from garuda.workspace.tmux import TmuxEnvironment


@pytest.fixture
def private_tmux_server(tmp_path, monkeypatch):
    tmux = shutil.which("tmux")
    if tmux is None:
        pytest.skip("tmux not installed")
    # Unix socket paths must stay short, independently of pytest's long paths.
    socket_dir = tempfile.TemporaryDirectory(prefix="g-tmux-", dir="/tmp")
    monkeypatch.setenv("TMUX_TMPDIR", socket_dir.name)
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.setenv("HISTFILE", str(tmp_path / "history"))

    def run(*args):
        return subprocess.run([tmux, *args], capture_output=True, text=True, check=True)

    run("-f", "/dev/null", "new-session", "-d", "-s", "test-bootstrap", "sleep 120")

    def configure(shell):
        executable = shutil.which(shell)
        if executable is None:
            pytest.skip(f"{shell} not installed")
        run("set-option", "-g", "default-shell", executable)
        options = "--noprofile --norc" if shell == "bash" else "-f"
        run("set-option", "-g", "default-command", f"{executable} {options}")

    try:
        yield configure
    finally:
        subprocess.run([tmux, "kill-server"], capture_output=True)
        socket_dir.cleanup()


async def test_tmux_uses_bash_comments_despite_zsh_server_default(tmp_path, private_tmux_server):
    private_tmux_server("zsh")
    workspace = tmp_path / "workspace"
    env = TmuxEnvironment(workspace)
    try:
        result = await env.send_command("echo before #; touch must-not-run", timeout=3)
        assert not (workspace / "must-not-run").exists()
        assert result.exit_code == 0, result.stderr
    finally:
        await env.stop()


async def test_tmux_does_not_expand_history_into_a_denied_path(tmp_path, private_tmux_server):
    private_tmux_server("bash")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / ".env").write_text("DENIED-HISTORY-CANARY")
    (workspace / "!-2:1!-1:1").write_text("LITERAL-HISTORY-OPERAND")
    env = TmuxEnvironment(workspace)
    try:
        flags = await env.send_command("set -o | grep '^histexpand'", timeout=3)
        assert "off" in flags.stdout.split()
        await env.send_command("echo .e", timeout=3)
        await env.send_command("echo nv", timeout=3)
        result = await env.send_command("cat !-2:1!-1:1", timeout=3)
        assert result.exit_code == 0, result.stderr
        assert "LITERAL-HISTORY-OPERAND" in result.stdout
        assert "DENIED-HISTORY-CANARY" not in result.stdout
    finally:
        await env.stop()


async def test_tmux_frame_preserves_shell_state_after_a_comment(tmp_path, private_tmux_server):
    private_tmux_server("bash")
    workspace = tmp_path / "workspace"
    (workspace / "sub").mkdir(parents=True)
    env = TmuxEnvironment(workspace)
    try:
        changed = await env.send_command("cd sub; export KEPT=present # keep state", timeout=3)
        assert changed.exit_code == 0, changed.stderr
        result = await env.send_command('printf "%s\\n" "$KEPT"; pwd', timeout=3)
        assert result.exit_code == 0, result.stderr
        assert "present" in result.stdout
        assert str((workspace / "sub").resolve()) in result.stdout
    finally:
        await env.stop()
