import asyncio
import time
from pathlib import Path

from garuda.types import ExecResult


class LocalEnvironment:
    def __init__(
        self,
        workspace_root: str | Path | None = None,
        confine_to_workspace: bool = True,
    ):
        self._workspace_root = Path(workspace_root or Path.cwd()).resolve()
        self._confine_to_workspace = confine_to_workspace

    @property
    def workspace_root(self) -> str:
        return str(self._workspace_root)

    def _resolve_path(self, path: str) -> Path:
        candidate = Path(path)
        if not candidate.is_absolute():
            candidate = self._workspace_root / candidate
        resolved = candidate.resolve()
        if self._confine_to_workspace and not resolved.is_relative_to(self._workspace_root):
            raise PermissionError(
                f"Path {path} is outside the workspace root {self._workspace_root}. "
                "Pass an explicit workspace or disable confinement."
            )
        return resolved

    async def execute(
        self,
        command: str,
        timeout: float | None = 120.0,
        cwd: str | None = None,
    ) -> ExecResult:
        workdir = Path(cwd) if cwd else self._workspace_root
        start = time.monotonic()

        process = await asyncio.create_subprocess_shell(
            command,
            cwd=str(workdir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                process.communicate(),
                timeout=timeout,
            )
        except (TimeoutError, asyncio.TimeoutError):
            process.kill()
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

    async def read_file(self, path: str) -> str:
        target = self._resolve_path(path)
        return await asyncio.to_thread(target.read_text, encoding="utf-8")

    async def write_file(self, path: str, content: str) -> None:
        target = self._resolve_path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(target.write_text, content, encoding="utf-8")
