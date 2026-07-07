"""Tool registry: a process-wide base of built-in tools plus per-run layers.

Built-ins live in one shared base registry — they're stateless singletons, safe
to share across runs. A run that needs custom or overriding tools builds a
*layered* registry on top of the base, so its additions never leak into other
runs sharing the same process (a long-lived server or batch eval). The
module-level functions operate on the shared base for backward compatibility.
"""

from __future__ import annotations

from garuda.tools.protocol import Tool


class ToolRegistry:
    """A name→Tool map, optionally layered over a read-through base registry."""

    def __init__(self, base: "ToolRegistry | None" = None):
        self._tools: dict[str, Tool] = {}
        self._base = base

    def register(self, tool: Tool, *, replace: bool = False) -> None:
        """Register a tool by name. Set replace=True to override an existing one."""
        if not replace and tool.name in self._tools:
            raise ValueError(f"Tool already registered: {tool.name}")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        tool = self._tools.get(name)
        if tool is not None:
            return tool
        return self._base.get(name) if self._base else None

    def names(self) -> list[str]:
        seen = set(self._tools)
        if self._base is not None:
            seen.update(self._base.names())
        return sorted(seen)

    def all_tools(self) -> list[Tool]:
        """Every resolvable tool; this layer's entries override the base by name.

        Base entries keep their insertion order (so a layer-free base preserves
        the original bootstrap order); this layer's own additions follow.
        """
        resolved: dict[str, Tool] = {}
        if self._base is not None:
            for tool in self._base.all_tools():
                resolved[tool.name] = tool
        resolved.update(self._tools)
        return list(resolved.values())

    def select(self, names: list[str] | None) -> list[Tool]:
        """Resolve an ordered list of tool names (None = all resolvable tools).

        Unknown names and ``mcp__*`` names are skipped here — MCP tools are
        appended separately by :func:`build_toolkit` from the live client manager.
        """
        if names is None:
            return self.all_tools()
        tools: list[Tool] = []
        for name in names:
            tool = self.get(name)
            if tool is not None:
                tools.append(tool)
        return tools

    def layer(self) -> "ToolRegistry":
        """A fresh child registry that reads through to this one."""
        return ToolRegistry(base=self)

    def clear(self) -> None:
        self._tools.clear()


# Process-wide base registry, seeded with built-ins at import time by
# ``garuda.tools.__init__._bootstrap_registry``.
_default = ToolRegistry()


def builtin_registry() -> ToolRegistry:
    """The shared base registry of built-in tools."""
    return _default


# --- Module-level back-compat API (operates on the shared base) ---------------


def register_tool(tool: Tool, *, replace: bool = False) -> None:
    """Register a tool into the shared base. Set replace=True to override built-ins."""
    _default.register(tool, replace=replace)


def get_tool(name: str) -> Tool | None:
    return _default.get(name)


def list_tool_names() -> list[str]:
    return _default.names()


def tools_for_names(names: list[str] | None) -> list[Tool]:
    return _default.select(names)


def clear_registry() -> None:
    _default.clear()
