import asyncio
import contextlib
import os
import signal
import time
from pathlib import Path

from garuda.types import ExecResult
from garuda.workspace.paths import resolve_workspace_path


def _kill_process_tree(process: "asyncio.subprocess.Process") -> None:
    """Kill the whole process group so a timed-out command's children die too.

    The subprocess is launched as a session leader (start_new_session=True), so its
    PID is its process-group id; killpg reaps grandchildren (node/pytest/etc.) that
    a bare process.kill() would orphan (holding ports/locks/CPU for the rest of the run).
    """
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        try:
            process.kill()
        except ProcessLookupError:
            pass


# Seconds allowed to finish reading a pipe. After a kill both pipes are at EOF so
# this returns at once; the bound only stops a grandchild that inherited the pipe
# and is still holding it open from re-hanging the turn.
_PIPE_DRAIN_TIMEOUT = 5.0


async def _collect(task: "asyncio.Task[bytes]") -> bytes:
    """Await a pipe-reader task, yielding b"" rather than raising."""
    try:
        return await asyncio.wait_for(task, timeout=_PIPE_DRAIN_TIMEOUT)
    except (TimeoutError, asyncio.TimeoutError):
        task.cancel()
        return b""
    except Exception:
        return b""


class LocalEnvironment:
    def __init__(
        self,
        workspace_root: str | Path | None = None,
        confine_to_workspace: bool = True,
    ):
        self._workspace_root = Path(workspace_root or Path.cwd()).resolve()
        self._confine_to_workspace = confine_to_workspace
        self._shell = None  # lazily-created PersistentShell (opt-in stateful bash)

    @property
    def workspace_root(self) -> str:
        return str(self._workspace_root)

    def _resolve_path(self, path: str) -> Path:
        return resolve_workspace_path(self._workspace_root, path, self._confine_to_workspace)

    async def execute(
        self,
        command: str,
        timeout: float | None = 120.0,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
    ) -> ExecResult:
        # A relative cwd resolves against the workspace root (not the harness
        # process cwd), matching docker/remote semantics; an absolute cwd is honored
        # as-is for the trusted local env.
        if cwd:
            cwd_path = Path(cwd)
            workdir = cwd_path if cwd_path.is_absolute() else (self._workspace_root / cwd_path)
        else:
            workdir = self._workspace_root
        start = time.monotonic()

        process = await asyncio.create_subprocess_shell(
            command,
            cwd=str(workdir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            start_new_session=True,  # own process group, so timeout can kill children
        )
        # Read the pipes in their own tasks rather than via communicate(). A
        # wait_for around communicate() *cancels* it on timeout, which abandons the
        # readers and loses everything the command had already written — and a build
        # or test run that burned its whole budget and then died is exactly when its
        # output matters most. Draining separately keeps the partial output.
        stdout_task = asyncio.create_task(process.stdout.read())
        stderr_task = asyncio.create_task(process.stderr.read())
        try:
            await asyncio.wait_for(process.wait(), timeout=timeout)
        except (TimeoutError, asyncio.TimeoutError):
            _kill_process_tree(process)
            with contextlib.suppress(ProcessLookupError):
                await process.wait()
            # The process is dead, so both pipes are at EOF and these return at once.
            partial_out = await _collect(stdout_task)
            partial_err = await _collect(stderr_task)
            duration_ms = int((time.monotonic() - start) * 1000)
            notice = f"Command timed out after {timeout}s and was killed."
            return ExecResult(
                stdout=partial_out.decode(errors="replace"),
                stderr=(
                    f"{partial_err.decode(errors='replace')}\n{notice}"
                    if partial_err
                    else notice
                ),
                exit_code=124,
                duration_ms=duration_ms,
                truncated=True,
            )
        stdout_bytes = await _collect(stdout_task)
        stderr_bytes = await _collect(stderr_task)
        duration_ms = int((time.monotonic() - start) * 1000)
        return ExecResult(
            stdout=stdout_bytes.decode(errors="replace"),
            stderr=stderr_bytes.decode(errors="replace"),
            exit_code=process.returncode or 0,
            duration_ms=duration_ms,
        )

    async def persistent_execute(
        self, command: str, timeout: float | None = 120.0, cwd: str | None = None
    ) -> ExecResult:
        """Run in a long-lived shell that keeps cwd/env/venv across calls.

        A per-call ``cwd`` runs in a subshell so it doesn't move the session's own
        working directory (matching the stateless tool's per-call cwd semantics).
        """
        if self._shell is None:
            import os

            from garuda.workspace.shell import PersistentShell

            self._shell = PersistentShell(cwd=str(self._workspace_root), env=dict(os.environ))
        if cwd:
            command = f"( cd {Path(cwd).as_posix()!r} && {command} )"
        return await self._shell.run(command, timeout=timeout)

    async def aclose(self) -> None:
        """Tear down the persistent shell if one was created."""
        if self._shell is not None:
            await self._shell.close()
            self._shell = None

    async def read_file(self, path: str) -> str:
        target = self._resolve_path(path)
        return await asyncio.to_thread(target.read_text, encoding="utf-8")

    async def write_file(self, path: str, content: str) -> None:
        target = self._resolve_path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(target.write_text, content, encoding="utf-8")
