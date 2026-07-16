"""Lazy tool discovery (Feature A) — `search_tool` / `use_tool`.

When many tools are available (typically several MCP servers), putting every tool
schema into the prompt is expensive — it inflates every request by hundreds to
thousands of tokens the model rarely needs. Instead of listing them all, expose two
small meta-tools:

* ``search_tool(query)`` — find tools by keyword (name/description), returning a
  compact name + description + argument summary the model can act on.
* ``use_tool(name, arguments)`` — invoke a discovered tool by name.

``build_toolkit`` swaps the raw tool list for these two when the count crosses a
threshold, so the prompt stays lean without losing any capability.
"""

from garuda.tools.protocol import Tool, ToolContext
from garuda.types import ToolResult
from garuda.workspace.protocol import Environment

_DEFAULT_SEARCH_LIMIT = 10
_MAX_DESC_CHARS = 200
_MAX_BROWSE = 40


def _summarize_params(schema: dict | None) -> str:
    """One-line argument summary from a JSON-schema parameters block."""
    props = (schema or {}).get("properties") or {}
    if not props:
        return "(no arguments)"
    required = set((schema or {}).get("required") or [])
    parts: list[str] = []
    for name, spec in props.items():
        kind = spec.get("type", "any") if isinstance(spec, dict) else "any"
        parts.append(f"{name} ({kind}{', required' if name in required else ''})")
    return ", ".join(parts)


def _describe(tool: Tool) -> str:
    desc = (tool.description or "").strip().replace("\n", " ")
    if len(desc) > _MAX_DESC_CHARS:
        desc = desc[:_MAX_DESC_CHARS].rstrip() + "…"
    return f"{tool.name} — {desc}\n  args: {_summarize_params(getattr(tool, 'parameters', None))}"


class SearchToolTool:
    name = "search_tool"
    description = (
        "Search for available tools by keyword (matches tool names and descriptions). "
        "Many tools — especially MCP server tools — are discoverable this way instead of "
        "being listed up front, which keeps your context small. Returns matching tool "
        "names, descriptions, and argument lists; then call use_tool with the chosen name. "
        "Search with a broad term (or an empty query) to browse what is available."
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Keyword(s) to match against tool names/descriptions (empty = browse all)",
            },
            "limit": {
                "type": "integer",
                "description": f"Max tools to return (default {_DEFAULT_SEARCH_LIMIT})",
            },
        },
        "required": ["query"],
    }

    def __init__(self, tools: dict[str, Tool]):
        # Shared name->tool map (the same dict backs use_tool).
        self._tools = tools

    async def execute(self, arguments: dict, env: Environment, ctx: ToolContext) -> ToolResult:
        query = (arguments.get("query") or "").strip().lower()
        limit = arguments.get("limit") or _DEFAULT_SEARCH_LIMIT
        if limit < 1:
            limit = 1

        tools = list(self._tools.values())
        if query:
            matches = [
                t
                for t in tools
                if query in t.name.lower() or query in (t.description or "").lower()
            ]
        else:
            matches = tools

        if not matches:
            names = ", ".join(sorted(self._tools)[:_MAX_BROWSE]) or "(none)"
            return ToolResult(
                tool_call_id="",
                content=(
                    f"No tool matched {query!r}. Available tools ({len(self._tools)}): {names}. "
                    "Try a broader query, or call use_tool with an exact name."
                ),
            )

        shown = matches[:limit]
        body = "\n".join(_describe(t) for t in shown)
        header = f"{len(matches)} tool(s) match {query!r}" if query else f"{len(matches)} tool(s) available"
        if len(matches) > len(shown):
            header += f" (showing {len(shown)}; refine the query for more)"
        return ToolResult(
            tool_call_id="",
            content=f"{header}:\n{body}\n\nInvoke one with use_tool(name=..., arguments={{...}}).",
        )


class UseToolTool:
    name = "use_tool"
    description = (
        "Invoke a tool by name (typically one found via search_tool), passing its arguments "
        "as an object. Use this for tools that are not in your direct tool list."
    )
    parameters = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Exact tool name to invoke"},
            "arguments": {
                "type": "object",
                "description": "Arguments object for the tool (see search_tool's arg list)",
            },
        },
        "required": ["name", "arguments"],
    }

    def __init__(self, tools: dict[str, Tool]):
        self._tools = tools

    async def execute(self, arguments: dict, env: Environment, ctx: ToolContext) -> ToolResult:
        name = arguments.get("name")
        if not name or not isinstance(name, str):
            return ToolResult(
                tool_call_id="", content="use_tool requires a 'name' string.", is_error=True
            )
        tool = self._tools.get(name)
        if tool is None:
            hint = ", ".join(sorted(self._tools)[:_MAX_BROWSE]) or "(none)"
            return ToolResult(
                tool_call_id="",
                content=(
                    f"Unknown tool {name!r}. Use search_tool to find the exact name. "
                    f"Available: {hint}."
                ),
                is_error=True,
            )
        tool_args = arguments.get("arguments") or {}
        if not isinstance(tool_args, dict):
            return ToolResult(
                tool_call_id="",
                content="'arguments' must be an object (a JSON dict of the tool's parameters).",
                is_error=True,
            )
        return await tool.execute(tool_args, env, ctx)
