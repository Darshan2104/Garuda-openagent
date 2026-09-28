from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from garuda.model.protocol import Model
from garuda.types import ToolResult
from garuda.workspace.protocol import Environment

if TYPE_CHECKING:
    from garuda.core.buffer import ToolOutputBuffer
    from garuda.core.collection import CollectionCoordinator
    from garuda.core.permissions import PermissionEngine
    from garuda.core.subagent import SubagentRunner


class ToolEffect(str, Enum):
    """Conservative effect categories used by restricted tool policies.

    A declaration describes the broadest effect a tool invocation may have.  It
    is metadata, not proof: authority-sensitive policies must also establish who
    supplied the declaration before trusting it.
    """

    READ_ONLY = "read_only"
    EXTERNAL_READ = "external_read"
    MUTATING = "mutating"
    EXTERNAL_SIDE_EFFECT = "external_side_effect"
    UNKNOWN = "unknown"


def tool_effect(tool: object) -> ToolEffect:
    """Return a tool's declared effect, failing closed to ``UNKNOWN``."""
    raw = getattr(tool, "effect", ToolEffect.UNKNOWN)
    try:
        return raw if isinstance(raw, ToolEffect) else ToolEffect(raw)
    except (TypeError, ValueError):
        return ToolEffect.UNKNOWN


@dataclass
class ToolContext:
    session_id: str
    agent_profile: str = "build"
    model: Model | None = None
    subagent_runner: "SubagentRunner | None" = None
    # Present only on an enabled dual-model native run. The public delegation
    # tool refuses when this trusted, run-scoped coordinator is absent.
    collection_coordinator: "CollectionCoordinator | None" = None
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
    # A dynamic dispatch ceiling for meta-tools such as ``use_tool``. None means
    # the ordinary run has no effect restriction; a collection child supplies a
    # fail-closed set and the meta-tool re-checks its selected underlying tool.
    allowed_tool_effects: frozenset[ToolEffect] | None = None


# Attribute name marking a tool the caller supplied explicitly — via
# `SoftwareAgent.register_tool`, `build_toolkit(extra_tools=...)`, or an opt-in
# `.agent/tools` module — rather than one discovered from the built-in registry.
#
# A profile's `tools:` allowlist selects among *discovered* tools; it must not
# discard one the caller handed in, and it is applied in two independent places
# (`build_toolkit` and `run_state._filter_tools`). Marking the tool is what lets
# both honour that without either needing to know how the other was called.
# Defined here, on the protocol module both sides already import, so neither has
# to import the other.
EXPLICIT_TOOL_ATTR = "_garuda_explicit"


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
