from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from garuda.model.protocol import Model
from garuda.types import ToolResult
from garuda.workspace.protocol import Environment


@dataclass
class ToolContext:
    session_id: str
    agent_profile: str = "build"
    model: Model | None = None


@runtime_checkable
class Tool(Protocol):
    name: str
    description: str
    parameters: dict[str, Any]

    async def execute(
        self,
        arguments: dict[str, Any],
        env: Environment,
        ctx: ToolContext,
    ) -> ToolResult: ...
