"""A long-lived shell session that preserves state across commands.

Unlike a fresh subprocess per call, this keeps ONE `bash` process alive so cwd,
environment variables, and activated virtualenvs persist between `bash` tool
calls — matching how a developer's terminal behaves. Commands are framed with a
unique end marker so output and the exit code are captured reliably; stderr is
merged into stdout (a terminal shows both). On timeout the running command is
interrupted (SIGINT to the process group); if the shell is wedged it is killed
and respawned (state is lost only on that failure path).
"""

import asyncio
import os
import shlex
import signal
import time
import uuid

from garuda.types import ExecResult


class PersistentShell:
    def __init__(self, cwd: str, env: dict[str, str] | None = None):
        self._cwd = cwd
        self._env = env
        self._proc: asyncio.subprocess.Process | None = None
        self._lock = asyncio.Lock()

    async def _ensure_started(self) -> None:
        if self._proc is not None and self._proc.returncode is None:
            return
        self._proc = await asyncio.create_subprocess_exec(
            "bash",
            "--norc",
            "--noprofile",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,  # merge; a terminal interleaves both
            cwd=self._cwd,
            env=self._env,
            start_new_session=True,  # own process group, so SIGINT/kill hit children too
        )

    async def run(self, command: str, timeout: float | None = 120.0) -> ExecResult:
        async with self._lock:  # one command at a time on a single shared shell
            return await self._run_locked(command, timeout)

    async def _run_locked(self, command: str, timeout: float | None) -> ExecResult:
        await self._ensure_started()
        assert self._proc is not None and self._proc.stdin is not None
        marker = f"__GARUDA_END_{uuid.uuid4().hex}__"
        # Run the command, capture its exit code, then print the marker + code on
        # its own line (leading \n guarantees the marker isn't glued to output).
        payload = f"{command}\n__ec=$?; printf '\\n%s %d\\n' {shlex.quote(marker)} \"$__ec\"\n"
        start = time.monotonic()
        try:
            self._proc.stdin.write(payload.encode("utf-8", errors="replace"))
            await self._proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            await self._restart()
            return self._timeout_result("shell was not writable; restarted", start, exit_code=1)

        try:
            out, code = await asyncio.wait_for(self._read_until(marker), timeout=timeout)
            return ExecResult(
                stdout=out, stderr="", exit_code=code,
                duration_ms=int((time.monotonic() - start) * 1000),
            )
        except _ShellClosed:
            await self._restart()
            return self._timeout_result("shell exited unexpectedly; restarted", start, exit_code=1)
        except (asyncio.TimeoutError, TimeoutError):
            # Interrupt the running command; give it a moment to yield the marker.
            self._signal(signal.SIGINT)
            try:
                out, code = await asyncio.wait_for(self._read_until(marker), timeout=3.0)
                return ExecResult(
                    stdout=out,
                    stderr=f"(command timed out after {timeout}s and was interrupted)",
                    exit_code=124,
                    duration_ms=int((time.monotonic() - start) * 1000),
                    truncated=True,
                )
            except (asyncio.TimeoutError, TimeoutError, _ShellClosed):
                await self._restart()  # wedged — respawn (loses shell state)
                return self._timeout_result(
                    f"command timed out after {timeout}s; shell restarted", start, exit_code=124
                )

    async def _read_until(self, marker: str) -> tuple[str, int]:
        assert self._proc is not None and self._proc.stdout is not None
        lines: list[str] = []
        while True:
            raw = await self._proc.stdout.readline()
            if not raw:
                raise _ShellClosed()
            line = raw.decode("utf-8", errors="replace")
            if marker in line:
                parts = line.strip().split()
                code = int(parts[-1]) if parts and parts[-1].lstrip("-").isdigit() else 0
                text = "".join(lines)
                if text.endswith("\n"):
                    text = text[:-1]  # drop the newline we injected before the marker
                return text, code
            lines.append(line)

    def _signal(self, sig: int) -> None:
        if self._proc and self._proc.returncode is None:
            try:
                os.killpg(os.getpgid(self._proc.pid), sig)
            except (ProcessLookupError, PermissionError):
                pass

    def _timeout_result(self, msg: str, start: float, exit_code: int) -> ExecResult:
        return ExecResult(
            stdout="", stderr=msg, exit_code=exit_code,
            duration_ms=int((time.monotonic() - start) * 1000), truncated=True,
        )

    async def _restart(self) -> None:
        await self.close()
        await self._ensure_started()

    async def close(self) -> None:
        if self._proc is None:
            return
        self._signal(signal.SIGKILL)
        try:
            await asyncio.wait_for(self._proc.wait(), timeout=5.0)
        except (asyncio.TimeoutError, TimeoutError, ProcessLookupError):
            pass
        self._proc = None


class _ShellClosed(Exception):
    """Raised internally when the shell process has exited (EOF on stdout)."""
