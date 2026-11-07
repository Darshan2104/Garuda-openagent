from typing import Protocol, runtime_checkable

from garuda.types import ExecResult


class WorkspaceAccessDenied(PermissionError):
    """A workspace policy refused access, independently of OS file permissions."""


@runtime_checkable
class Environment(Protocol):
    async def execute(
        self,
        command: str,
        timeout: float | None = None,
        cwd: str | None = None,
    ) -> ExecResult: ...

    async def read_file(self, path: str) -> str: ...

    async def read_bytes(self, path: str, *, max_bytes: int | None = None) -> bytes:
        """Read binary content under the same path policy as read_file.

        With max_bytes, read at most max_bytes + 1 bytes and refuse overflow.
        Older environments without this optional capability cannot run codemods."""
        ...

    async def write_file(self, path: str, content: str) -> None: ...

    @property
    def workspace_root(self) -> str: ...
