from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from garuda.types import ToolCall, ToolResult


BeforeToolHook = Callable[[ToolCall, dict[str, Any]], Awaitable[ToolCall | None]]
AfterToolHook = Callable[[ToolCall, ToolResult, dict[str, Any]], Awaitable[ToolResult | None]]


@dataclass
class HookRegistry:
    before_tool: list[BeforeToolHook] = field(default_factory=list)
    after_tool: list[AfterToolHook] = field(default_factory=list)

    def register_before_tool(self, hook: BeforeToolHook) -> None:
        self.before_tool.append(hook)

    def register_after_tool(self, hook: AfterToolHook) -> None:
        self.after_tool.append(hook)

    async def run_before_tool(self, call: ToolCall, context: dict[str, Any]) -> ToolCall | None:
        current = call
        for hook in self.before_tool:
            current = await hook(current, context)
            if current is None:
                return None
        return current

    async def run_after_tool(
        self,
        call: ToolCall,
        result: ToolResult,
        context: dict[str, Any],
    ) -> ToolResult:
        current = result
        for hook in self.after_tool:
            updated = await hook(call, current, context)
            if updated is not None:
                current = updated
        return current
