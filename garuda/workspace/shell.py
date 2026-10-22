"""A long-lived shell session that preserves state across commands.

Unlike a fresh subprocess per call, this keeps ONE `bash` process alive so cwd,
environment variables, and activated virtualenvs persist between `bash` tool
calls — matching how a developer's terminal behaves. Commands are framed with a
unique end marker so output and the exit code are captured reliably; stderr is
merged into stdout (a terminal shows both). On timeout the running command is
interrupted (SIGINT to the process group); if the shell is wedged it is killed
and respawned (state is lost on that failure path). Cancellation signals the
live process group and settles the shell and pipes before another call can run;
that next call starts with fresh state.
"""

import asyncio
import os
import shlex
import signal
import time
import uuid

from garuda.types import ExecResult
from garuda.workspace.settlement import settle_task


class PersistentShell:
    def __init__(self, cwd: str, env: dict[str, str] | None = None):
        self._cwd = cwd
        self._env = env
        self._proc: asyncio.subprocess.Process | None = None
        self._lock = asyncio.Lock()
        self._cleanup_pending = False

    async def _ensure_started(self) -> None:
        if self._cleanup_pending:
            try:
                await self.close()
            except Exception as exc:
                raise RuntimeError("Previous shell cleanup is incomplete; refusing another command") from exc
        if self._proc is not None and self._proc.returncode is None:
            return
        launch = asyncio.create_task(asyncio.create_subprocess_exec(
            "bash",
            "--norc",
            "--noprofile",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,  # merge; a terminal interleaves both
            cwd=self._cwd,
            env=self._env,
            start_new_session=True,  # own process group, so SIGINT/kill hit children too
        ))
        try:
            self._proc = await asyncio.shield(launch)
        except asyncio.CancelledError as cancelled:
            # Retain ownership even when cancellation races process creation.
            try:
                self._proc = await settle_task(launch)
            except Exception as exc:
                raise cancelled from exc
            raise

    async def run(self, command: str, timeout: float | None = 120.0) -> ExecResult:
        async with self._lock:  # one command at a time on a single shared shell
            try:
                return await self._run_locked(command, timeout)
            except asyncio.CancelledError as cancelled:
                # Discard the cancelled frame and all its output before releasing
                # the lock. The next call starts a fresh shell, never a stale read.
                cleanup = asyncio.create_task(self.close())
                try:
                    await settle_task(cleanup)
                except Exception as exc:
                    raise cancelled from exc
                raise

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
        if self._proc is not None and self._proc.returncode is None:
            try:
                os.killpg(self._proc.pid, sig)
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
        self._cleanup_pending = True
        self._signal(signal.SIGKILL)
        try:
            await asyncio.wait_for(self._proc.wait(), timeout=5.0)
        except TimeoutError:
            if self._proc.returncode is None:
                raise  # Exit is still unproved; retain ownership for a retry.
            # The leader was reaped, but an escaped descendant retains stdout.
            # asyncio Process.wait() can wait for pipe EOF even after exit.
        except ProcessLookupError:
            pass
        # Close owned pipes even if the leader exited before close() was called.
        # Process exposes no public pipe-close API; use its subprocess transport.
        # A recorded exit prevents signaling the leader's reusable process-group id.
        transport = self._proc._transport
        for fd in (0, 1, 2):
            pipe = transport.get_pipe_transport(fd)
            if pipe is not None:
                pipe.close()
        self._proc = None
        self._cleanup_pending = False


class _ShellClosed(Exception):
    """Raised internally when the shell process has exited (EOF on stdout)."""
