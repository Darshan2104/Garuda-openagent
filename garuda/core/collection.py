"""Collection-worker tool policy.

This module builds a deliberately narrow, non-mutating toolkit.  It is a local
guardrail, not a confinement boundary: strict write isolation still requires a
read-only mount or an isolated workspace snapshot.
"""

from __future__ import annotations

from dataclasses import dataclass

from garuda.core.evidence import is_side_effect_free
from garuda.core.permissions import PermissionDecision, PermissionEngine
from garuda.tools.protocol import Tool, ToolContext, ToolEffect, tool_effect
from garuda.tools.registry import builtin_registry
from garuda.types import ToolResult
from garuda.workspace.protocol import Environment

DEFAULT_COLLECTION_TOOL_NAMES = frozenset(
    {
        "read_file",
        "grep",
        "glob",
        "ls",
        "read_pdf",
        "read_spreadsheet",
        "image_read",
        "buffer_grep",
        "buffer_slice",
        "buffer_list",
        "buffer_query",
        "web_fetch",
        "web_search",
    }
)

# Names that remain forbidden even if a malicious profile supplies an object
# carrying a forged read-only declaration. Most are already excluded by effect;
# the name ceiling is defense in depth for control-plane and delegation tools.
COLLECTION_HARD_DENY = frozenset(
    {
        "write_file",
        "edit",
        "multi_edit",
        "notebook_edit",
        "bash_background",
        "task_output",
        "kill_task",
        "tmux_exec",
        "tmux_capture",
        "todo",
        "update_goal",
        "contract",
        "task_complete",
        "invoke_subagent",
        "delegate_collection",
    }
)


@dataclass(frozen=True)
class CollectionPolicy:
    """The independent selectors whose intersection forms a worker toolkit."""

    profile_tools: frozenset[str] = DEFAULT_COLLECTION_TOOL_NAMES
    requested_tools: frozenset[str] | None = None
    global_tools: frozenset[str] = DEFAULT_COLLECTION_TOOL_NAMES
    network_enabled: bool = False
    allow_readonly_shell: bool = False


class ReadOnlyCollectionShell:
    """A fail-closed shell facade for the optional collection shell capability."""

    name = "bash"
    effect = ToolEffect.READ_ONLY
    description = (
        "Run one conservatively classified read-only inspection command. "
        "Unrecognized programs, writes, backgrounding, and ambiguous composition are denied."
    )

    def __init__(self, delegate: Tool):
        self._delegate = delegate
        self.parameters = delegate.parameters

    async def execute(
        self, arguments: dict, env: Environment, ctx: ToolContext
    ) -> ToolResult:
        command = arguments.get("command") if isinstance(arguments, dict) else None
        if not isinstance(command, str) or not is_side_effect_free(command):
            return ToolResult(
                tool_call_id="",
                content=(
                    "Collection shell denied: command is not a recognized "
                    "side-effect-free inspection."
                ),
                is_error=True,
            )
        return await self._delegate.execute(arguments, env, ctx)


def trusted_collection_effect(tool: Tool) -> ToolEffect:
    """Return an effect only when its declaration has trusted provenance.

    Built-ins are trusted by object identity against the base registry, so a
    project tool cannot impersonate ``read_file`` merely by copying its name or
    declaring ``READ_ONLY``. MCP declarations are trusted only when the client
    marked the remote tool from the user-owned global configuration.
    """
    if isinstance(tool, ReadOnlyCollectionShell):
        return ToolEffect.READ_ONLY

    builtin = builtin_registry().get(tool.name)
    if builtin is tool:
        return tool_effect(tool)

    try:
        from garuda.mcp.client import McpRemoteTool
    except ImportError:
        McpRemoteTool = ()  # type: ignore[assignment,misc]
    if type(tool) is McpRemoteTool and tool.effect_trusted:
        return tool_effect(tool)
    return ToolEffect.UNKNOWN


def build_collection_toolkit(
    tools: list[Tool],
    policy: CollectionPolicy,
    *,
    permissions: PermissionEngine | None = None,
) -> list[Tool]:
    """Apply every collection selector and preserve the caller's tool order."""
    selected: list[Tool] = []
    seen: set[str] = set()
    for candidate in tools:
        name = candidate.name
        if name in seen or name in COLLECTION_HARD_DENY:
            continue
        if name not in policy.profile_tools:
            continue
        if policy.requested_tools is not None and name not in policy.requested_tools:
            continue

        tool: Tool = candidate
        is_readonly_shell = name == "bash" and policy.allow_readonly_shell
        if name == "bash":
            if not policy.allow_readonly_shell:
                continue
            # Never put a project-supplied object behind the trusted wrapper: its
            # execute method could ignore the screened command entirely.
            if builtin_registry().get("bash") is not candidate:
                continue
            tool = ReadOnlyCollectionShell(candidate)
            effect = ToolEffect.READ_ONLY
        else:
            effect = trusted_collection_effect(candidate)
            # Globally trusted MCP tools are exact dynamic additions to the
            # ceiling; ordinary/custom tools must also be named globally.
            is_trusted_mcp = effect != ToolEffect.UNKNOWN and getattr(
                candidate, "effect_trusted", False
            )
            if (
                name not in policy.global_tools
                and not is_trusted_mcp
                and not is_readonly_shell
            ):
                continue

        if effect == ToolEffect.EXTERNAL_READ and not policy.network_enabled:
            continue
        if effect not in (ToolEffect.READ_ONLY, ToolEffect.EXTERNAL_READ):
            continue
        if permissions is not None and (
            permissions.check_tool(name) is not PermissionDecision.ALLOW
        ):
            continue
        selected.append(tool)
        seen.add(name)
    return selected
