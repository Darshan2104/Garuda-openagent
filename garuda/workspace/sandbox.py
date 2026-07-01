"""OS-level sandbox wrapper using bubblewrap on Linux."""

import shlex
import shutil
from pathlib import Path

from garuda.types import ExecResult
from garuda.workspace.local import LocalEnvironment


class SandboxEnvironment:
    """Restrict command execution with bubblewrap when available."""

    def __init__(self, workspace_root: str | Path | None = None):
        self._inner = LocalEnvironment(workspace_root=workspace_root)
        self._workspace_root = self._inner.workspace_root
        self._bwrap = shutil.which("bwrap")

    @property
    def workspace_root(self) -> str:
        return self._workspace_root

    def is_sandboxed(self) -> bool:
        return self._bwrap is not None

    def _wrap_command(self, command: str, cwd: str | None) -> str:
        workdir = cwd or self._workspace_root
        if not self._bwrap:
            return command
        return (
            f"{shlex.quote(self._bwrap)} --die-with-parent --unshare-pid --unshare-uts "
            f"--ro-bind / / --bind {shlex.quote(workdir)} {shlex.quote(workdir)} "
            f"--dev /dev --proc /proc "
            f"--chdir {shlex.quote(workdir)} "
            f"/bin/bash -lc {shlex.quote(command)}"
        )

    async def execute(
        self,
        command: str,
        timeout: float | None = 120.0,
        cwd: str | None = None,
    ) -> ExecResult:
        wrapped = self._wrap_command(command, cwd)
        return await self._inner.execute(wrapped, timeout=timeout, cwd=self._workspace_root)

    async def read_file(self, path: str) -> str:
        return await self._inner.read_file(path)

    async def write_file(self, path: str, content: str) -> None:
        return await self._inner.write_file(path, content)
