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

    async def write_file(self, path: str, content: str) -> None: ...

    @property
    def workspace_root(self) -> str: ...
