"""Dynamic tool registry for built-in and plugin tools."""

from garuda.tools.protocol import Tool

_REGISTRY: dict[str, Tool] = {}


def register_tool(tool: Tool, *, replace: bool = False) -> None:
    """Register a tool by name. Set replace=True to override built-ins."""
    if tool.name in _REGISTRY and not replace:
        raise ValueError(f"Tool already registered: {tool.name}")
    _REGISTRY[tool.name] = tool


def get_tool(name: str) -> Tool | None:
    return _REGISTRY.get(name)


def list_tool_names() -> list[str]:
    return sorted(_REGISTRY.keys())


def tools_for_names(names: list[str] | None) -> list[Tool]:
    if names is None:
        return list(_REGISTRY.values())
    tools: list[Tool] = []
    for name in names:
        if name in _REGISTRY:
            tools.append(_REGISTRY[name])
        elif name.startswith("mcp__"):
            continue
    return tools


def clear_registry() -> None:
    _REGISTRY.clear()
