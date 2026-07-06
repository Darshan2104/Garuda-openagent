from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from garuda.model.protocol import Model
from garuda.types import ToolResult
from garuda.workspace.protocol import Environment

if TYPE_CHECKING:
    from garuda.core.buffer import ToolOutputBuffer
    from garuda.core.subagent import SubagentRunner


@dataclass
class ToolContext:
    session_id: str
    agent_profile: str = "build"
    model: Model | None = None
    subagent_runner: "SubagentRunner | None" = None
    buffer: "ToolOutputBuffer | None" = None
    post_edit_diagnostics: bool = True
    persistent_shell: bool = False


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
