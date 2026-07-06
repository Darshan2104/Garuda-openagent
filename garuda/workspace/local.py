import asyncio
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
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                process.communicate(),
                timeout=timeout,
            )
        except (TimeoutError, asyncio.TimeoutError):
            _kill_process_tree(process)
            try:
                await process.wait()
            except ProcessLookupError:
                pass
            duration_ms = int((time.monotonic() - start) * 1000)
            return ExecResult(
                stdout="",
                stderr=f"Command timed out after {timeout}s and was killed.",
                exit_code=124,
                duration_ms=duration_ms,
                truncated=True,
            )
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
