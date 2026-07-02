import asyncio
import json
import logging
import re
from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from garuda.mcp.config import McpServerConfig, load_mcp_config
from garuda.tools.protocol import Tool, ToolContext
from garuda.types import ToolResult

logger = logging.getLogger(__name__)

# Timeout (seconds) for each server's initialize() and list_tools() handshakes.
MCP_HANDSHAKE_TIMEOUT = 10.0

# Maximum length of an exposed MCP tool name.
MCP_TOOL_NAME_MAX_LEN = 64

_TOOL_NAME_SAFE = re.compile(r"[^a-zA-Z0-9_-]")


def sanitize_tool_name(raw: str) -> str:
    """Replace characters outside [a-zA-Z0-9_-] with _ and cap the length at 64."""
    return _TOOL_NAME_SAFE.sub("_", raw)[:MCP_TOOL_NAME_MAX_LEN]


@dataclass
class McpRemoteTool:
    server_name: str
    tool_name: str
    description: str
    parameters: dict[str, Any]
    _session: ClientSession

    @property
    def name(self) -> str:
        return sanitize_tool_name(f"mcp__{self.server_name}__{self.tool_name}")

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
        """Connect to each configured server. A failing server is logged and skipped
        so one broken server cannot take down the others."""
        if self._started:
            return
        for server in servers:
            if server.transport != "stdio":
                logger.warning(
                    "Skipping non-stdio MCP server %s (transport=%s)",
                    server.name,
                    server.transport,
                )
                continue
            try:
                await self._start_server(server)
            except Exception as exc:
                logger.warning(
                    "MCP server %s failed to start (%s: %s); skipping it",
                    server.name,
                    type(exc).__name__,
                    exc,
                )
        self._started = True

    async def _start_server(self, server: McpServerConfig) -> None:
        params = StdioServerParameters(
            command=server.command,
            args=server.args,
            env=server.env or None,
        )
        # Per-server exit stack: on failure only this server's resources are
        # torn down; on success ownership moves to the manager's stack.
        server_stack = AsyncExitStack()
        try:
            read, write = await server_stack.enter_async_context(stdio_client(params))
            session = await server_stack.enter_async_context(ClientSession(read, write))
            await asyncio.wait_for(session.initialize(), MCP_HANDSHAKE_TIMEOUT)
            listed = await asyncio.wait_for(session.list_tools(), MCP_HANDSHAKE_TIMEOUT)
        except BaseException:
            try:
                await server_stack.aclose()
            except Exception:
                # anyio can complain when tearing down a half-initialized
                # transport; the launch failure is what matters.
                logger.debug("Error closing failed MCP server %s", server.name, exc_info=True)
            raise
        await self._stack.enter_async_context(server_stack)
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

    def get_tools(self) -> list[Tool]:
        return list(self._tools)

    async def close(self) -> None:
        await self._stack.aclose()
        self._started = False
        self._tools = []
