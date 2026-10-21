from typing import Protocol, runtime_checkable

from garuda.types import ExecResult


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
