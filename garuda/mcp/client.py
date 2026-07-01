import json
from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from garuda.mcp.config import McpServerConfig, load_mcp_config
from garuda.tools.protocol import Tool, ToolContext
from garuda.types import ToolResult


@dataclass
class McpRemoteTool:
    server_name: str
    tool_name: str
    description: str
    parameters: dict[str, Any]
    _session: ClientSession

    @property
    def name(self) -> str:
        return f"mcp__{self.server_name}__{self.tool_name}"

    async def execute(
        self,
        arguments: dict[str, Any],
        env: object,
        ctx: ToolContext,
    ) -> ToolResult:
        result = await self._session.call_tool(self.tool_name, arguments)
        parts: list[str] = []
        for block in result.content:
            text = getattr(block, "text", None)
            if text:
                parts.append(text)
            else:
                parts.append(str(block))
        content = "\n".join(parts) if parts else json.dumps({"ok": True})
        return ToolResult(
            tool_call_id="",
            content=content,
            is_error=bool(result.isError),
        )


class McpClientManager:
    def __init__(self):
        self._stack = AsyncExitStack()
        self._tools: list[McpRemoteTool] = []
        self._started = False

    @classmethod
    async def from_config(cls, path: str) -> "McpClientManager":
        manager = cls()
        await manager.start(load_mcp_config(path))
        return manager

    async def start(self, servers: list[McpServerConfig]) -> None:
        if self._started:
            return
        for server in servers:
            if server.transport != "stdio":
                continue
            params = StdioServerParameters(
                command=server.command,
                args=server.args,
                env=server.env or None,
            )
            read, write = await self._stack.enter_async_context(stdio_client(params))
            session = await self._stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
            listed = await session.list_tools()
            for tool in listed.tools:
                schema = tool.inputSchema if hasattr(tool, "inputSchema") else {"type": "object", "properties": {}}
                self._tools.append(
                    McpRemoteTool(
                        server_name=server.name,
                        tool_name=tool.name,
                        description=tool.description or tool.name,
                        parameters=schema,
                        _session=session,
                    )
                )
        self._started = True

    def get_tools(self) -> list[Tool]:
        return list(self._tools)

    async def close(self) -> None:
        await self._stack.aclose()
        self._started = False
        self._tools = []
