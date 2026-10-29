from typing import Protocol, runtime_checkable

from garuda.types import ExecResult


class WorkspaceAccessDenied(PermissionError):
    """A workspace policy refused access, independently of OS file permissions."""


# Optional bounded UTF-8 range/tail capability: workspace.text_reads.BoundedTextEnvironment.
# It is separate so existing Environment implementations remain compatible.
@runtime_checkable
class Environment(Protocol):
    async def execute(
        self,
        command: str,
        timeout: float | None = None,
        cwd: str | None = None,
    ) -> ExecResult: ...

    async def read_file(self, path: str) -> str: ...

    async def read_bytes(self, path: str) -> bytes:
        """Read binary content under the same path policy as read_file."""
        ...

    async def write_file(self, path: str, content: str) -> None: ...

    @property
    def workspace_root(self) -> str: ...
