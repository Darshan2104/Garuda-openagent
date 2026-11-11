import asyncio
import contextlib
import os
import shlex
import signal
import time
from pathlib import Path
from typing import TYPE_CHECKING

from garuda.types import ExecResult
from garuda.workspace.paths import read_path_bytes, resolve_workspace_path
from garuda.workspace.settlement import settle_task
from garuda.workspace.text_reads import TextPage

if TYPE_CHECKING:
    from garuda.runtime.debug import DebugService


def _kill_process_tree(process: "asyncio.subprocess.Process") -> None:
    """Kill the issued process group, including after its leader has exited.

    The subprocess is launched as a session leader (start_new_session=True), so its
    PID is its process-group id; killpg kills grandchildren (node/pytest/etc.) that
    a bare process.kill() would orphan (holding ports/locks/CPU for the rest of the run).
    """
    try:
        os.killpg(process.pid, signal.SIGKILL)
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


async def _cancel_execution(
    launch: "asyncio.Task[asyncio.subprocess.Process]", readers: list[asyncio.Task[bytes]]
) -> None:
    """Settle a cancelled launch and all resources it actually created."""
    try:
        process = await launch
    except Exception:
        return  # launch failed before it produced a process to own
    try:
        _kill_process_tree(process)
        try:
            await asyncio.wait_for(process.wait(), timeout=_PIPE_DRAIN_TIMEOUT)
        except TimeoutError:
            if process.returncode is None:
                raise
            # An escaped descendant may retain a pipe after the leader was reaped.
    finally:
        for reader in readers:
            reader.cancel()
        await asyncio.gather(*readers, return_exceptions=True)


class LocalEnvironment:
    def __init__(
        self,
        workspace_root: str | Path | None = None,
        confine_to_workspace: bool = True,
    ):
        self._workspace_root = Path(workspace_root or Path.cwd()).resolve()
        self._confine_to_workspace = confine_to_workspace
        self._shell = None  # lazily-created PersistentShell (opt-in stateful bash)
        self._debug_service: DebugService | None = None
        self._debug_owners = {}

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

        launch = asyncio.create_task(asyncio.create_subprocess_shell(
            command,
            cwd=str(workdir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            start_new_session=True,  # own process group, so timeout can kill children
        ))
        # Read the pipes in their own tasks rather than via communicate(). A
        # wait_for around communicate() *cancels* it on timeout, which abandons the
        # readers and loses everything the command had already written — and a build
        # or test run that burned its whole budget and then died is exactly when its
        # output matters most. Draining separately keeps the partial output.
        readers = []
        try:
            # Retain launch ownership if cancellation arrives while the OS is spawning.
            process = await asyncio.shield(launch)
            stdout_task = asyncio.create_task(process.stdout.read())
            stderr_task = asyncio.create_task(process.stderr.read())
            readers.extend((stdout_task, stderr_task))
            timed_out = False
            try:
                await asyncio.wait_for(process.wait(), timeout=timeout)
            except (TimeoutError, asyncio.TimeoutError):
                timed_out = True
                _kill_process_tree(process)
                with contextlib.suppress(ProcessLookupError):
                    await process.wait()
            stdout_bytes = await _collect(stdout_task)
            stderr_bytes = await _collect(stderr_task)
            stderr = stderr_bytes.decode(errors="replace")
            if timed_out:
                notice = f"Command timed out after {timeout}s and was killed."
                stderr = f"{stderr}\n{notice}" if stderr else notice
            return ExecResult(
                stdout=stdout_bytes.decode(errors="replace"),
                stderr=stderr,
                exit_code=124 if timed_out else (process.returncode or 0),
                duration_ms=int((time.monotonic() - start) * 1000),
                truncated=timed_out,
            )
        except asyncio.CancelledError as cancelled:
            cleanup = asyncio.create_task(_cancel_execution(launch, readers))
            try:
                await settle_task(cleanup)
            except Exception as exc:
                # Keep cancellation as the public outcome without losing the
                # evidence that process cleanup could not be confirmed.
                raise cancelled from exc
            raise

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
            cwd_path = Path(cwd)
            workdir = cwd_path if cwd_path.is_absolute() else (self._workspace_root / cwd_path)
            command = f"(\ncd {shlex.quote(str(workdir))} && {{\n{command}\n}}\n)"
        return await self._shell.run(command, timeout=timeout)

    async def aclose(self) -> None:
        """Tear down optional services owned by this environment."""
        try:
            if self._debug_service is not None:
                await self._debug_service.close()
        finally:
            if self._shell is not None:
                await self._shell.close()
                self._shell = None

    def debug_service(self) -> "DebugService":
        from garuda.runtime.debug import LocalDebugService

        if self._debug_service is None:
            self._debug_service = LocalDebugService(self)
        return self._debug_service

    async def reap_debug_owners(self, session_id: str) -> list[str]:
        survivors = []
        for owner in list(self._debug_owners.get(session_id, [])):
            try:
                await owner.close()
            except Exception:
                survivors.append(f"debug:{owner.process.pid}: cleanup unproved")
            else:
                self._debug_owners[session_id].remove(owner)
        return survivors

    async def launch_debug_adapter(self, session_id: str):
        """Issue a local-only adapter inside an anchored, private POSIX session."""
        import sys

        from garuda.runtime.debug_adapter import DebugAdapter, DebugError
        from garuda.runtime.process_session import OwnedProcessSession, session_groups

        # Refuse before spawning if we cannot observe session closure here.
        await asyncio.to_thread(session_groups, os.getsid(0))
        self.debug_service()
        supervisor = (
            "import os, subprocess, sys; "
            "os.read(0, 1); "
            "p = subprocess.Popen([sys.executable, '-I', '-m', 'debugpy.adapter']); "
            "p.wait(); os.close(1); sys.stdin.buffer.read()"
        )

        async def start():
            process = await asyncio.create_subprocess_exec(
                sys.executable, "-I", "-c", supervisor, cwd=self.workspace_root,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, start_new_session=True,
                env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C.UTF-8",
                     "PYTHONUNBUFFERED": "1", "PYDEVD_DISABLE_FILE_VALIDATION": "1"},
            )
            owner = OwnedProcessSession(process)
            self._debug_owners.setdefault(session_id, []).append(owner)
            try:
                owner.bind()
            except Exception as exc:
                # No adapter can spawn before this ownership handshake.
                await owner.close()
                self._debug_owners[session_id].remove(owner)
                raise DebugError(f"debug launch refused: cannot bind process identity: {exc}") from exc
            owner.started = True
            process.stdin.write(b"G")
            return DebugAdapter(process, owner)

        launch = asyncio.create_task(start())
        try:
            return await asyncio.shield(launch)
        except asyncio.CancelledError as cancelled:
            try:
                adapter = await settle_task(launch)
                await settle_task(asyncio.create_task(adapter.close()))
            except Exception as exc:
                raise cancelled from exc
            raise

    async def read_file(self, path: str) -> str:
        target = self._resolve_path(path)
        return await asyncio.to_thread(target.read_text, encoding="utf-8")

    async def read_text_page(self, path: str, *, offset: int = 1,
                             limit: int = 2000, tail: bool = False,
                             digest: bool = False) -> TextPage:
        import threading

        from garuda.workspace.text_reads import read_local_page

        target = self._resolve_path(path)
        cancelled = threading.Event()
        task = asyncio.create_task(asyncio.to_thread(
            read_local_page, target, offset, limit, tail, cancelled, digest))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError as cancellation:
            cancelled.set()
            try:
                await settle_task(task)
            except InterruptedError:
                pass
            except Exception as exc:
                raise cancellation from exc
            raise

    async def read_bytes(self, path: str, *, max_bytes: int | None = None) -> bytes:
        target = self._resolve_path(path)
        return await asyncio.to_thread(read_path_bytes, target, max_bytes)

    async def write_file(self, path: str, content: str) -> None:
        target = self._resolve_path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(target.write_text, content, encoding="utf-8")
