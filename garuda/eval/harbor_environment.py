"""Bridge Harbor task environments to Garuda's Environment protocol."""

import shlex
import tempfile
from pathlib import Path

from garuda.types import ExecResult


class HarborEnvironmentAdapter:
    """Wrap a Harbor ``BaseEnvironment`` for Garuda tool execution."""

    def __init__(self, harbor_env: object, workspace_root: str | None = None):
        self._env = harbor_env
        self._workspace_root = workspace_root

    @property
    def workspace_root(self) -> str:
        if self._workspace_root:
            return self._workspace_root
        workdir = getattr(getattr(self._env, "task_env_config", None), "workdir", None)
        return workdir or "/app"

    def _resolve_path(self, path: str) -> str:
        if path.startswith("/"):
            return path
        root = self.workspace_root.rstrip("/")
        return f"{root}/{path}"

    async def execute(
        self,
        command: str,
        timeout: float | None = 120.0,
        cwd: str | None = None,
    ) -> ExecResult:
        workdir = cwd or self.workspace_root
        timeout_sec = int(timeout) if timeout is not None else None
        result = await self._env.exec(
            command=command,
            cwd=workdir,
            timeout_sec=timeout_sec,
        )
        return ExecResult(
            stdout=result.stdout or "",
            stderr=result.stderr or "",
            exit_code=result.return_code,
            duration_ms=0,
        )

    async def read_file(self, path: str) -> str:
        resolved = self._resolve_path(path)
        result = await self.execute(f"cat {shlex.quote(resolved)}")
        if result.exit_code != 0:
            raise FileNotFoundError(result.stderr or f"Cannot read {resolved}")
        return result.stdout

    async def write_file(self, path: str, content: str) -> None:
        resolved = self._resolve_path(path)
        parent = str(Path(resolved).parent)
        await self.execute(f"mkdir -p {shlex.quote(parent)}")
        with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".tmp", encoding="utf-8") as handle:
            handle.write(content)
            local_path = handle.name
        await self._env.upload_file(local_path, resolved)
        Path(local_path).unlink(missing_ok=True)

    async def resolve_workspace_root(self) -> str:
        """Detect the container working directory when not configured."""
        if self._workspace_root:
            return self._workspace_root
        workdir = getattr(getattr(self._env, "task_env_config", None), "workdir", None)
        if workdir:
            self._workspace_root = workdir
            return workdir
        result = await self._env.exec("pwd")
        if result.return_code == 0 and result.stdout:
            self._workspace_root = result.stdout.strip()
            return self._workspace_root
        self._workspace_root = "/app"
        return self._workspace_root
