from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from garuda.model.protocol import Model
from garuda.types import ToolResult
from garuda.workspace.protocol import Environment

if TYPE_CHECKING:
    from garuda.core.buffer import ToolOutputBuffer
    from garuda.core.permissions import PermissionEngine
    from garuda.core.subagent import SubagentRunner


@dataclass
class ToolContext:
    session_id: str
    agent_profile: str = "build"
    model: Model | None = None
    subagent_runner: "SubagentRunner | None" = None
    buffer: "ToolOutputBuffer | None" = None
    post_edit_diagnostics: bool = True
    post_edit_lint: bool = True
    persistent_shell: bool = False
    # Present so meta-tools that dispatch to other tools (e.g. use_tool) can re-run
    # the same permission screen the loop applies to a direct call. None disables the
    # check (callers that don't wire permissions get the pre-existing behavior).
    permissions: "PermissionEngine | None" = None
    # time.monotonic() at which the run's wall-clock budget expires, so tools that
    # block (bash) can bound themselves by what is actually left rather than by a
    # fixed per-call default. None means no wall-clock budget.
    deadline_monotonic: float | None = None
    # Largest share of the remaining budget one command may take.
    command_budget_fraction: float = 0.5
    # Run-scoped record of workspace mutations, for pre-completion cleanup.
    side_effects: Any = None
    # Acceptance criteria derived from the task statement.
    contract: Any = None


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
