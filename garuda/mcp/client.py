import asyncio
import json
import logging
import re
from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.sse import sse_client
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamablehttp_client

from garuda.mcp.config import (
    McpServerConfig,
    load_and_merge_mcp_configs,
    load_mcp_config,
    normalize_transport,
)
from garuda.tools.protocol import Tool, ToolContext
from garuda.types import ToolResult

logger = logging.getLogger(__name__)

# Timeout (seconds) for each server's initialize() and list_tools() handshakes.
MCP_HANDSHAKE_TIMEOUT = 10.0

# Connect/handshake timeout (seconds) for HTTP/SSE transports.
MCP_HTTP_CONNECT_TIMEOUT = 30.0

# Timeout (seconds) for a single MCP tool invocation.
MCP_CALL_TIMEOUT = 120.0

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
    name_override: str | None = None

    @property
    def name(self) -> str:
        if self.name_override:
            return self.name_override
        return sanitize_tool_name(f"mcp__{self.server_name}__{self.tool_name}")

    async def execute(
        self,
        arguments: dict[str, Any],
        env: object,
        ctx: ToolContext,
    ) -> ToolResult:
        # A hung or failing MCP server must not hang or crash the agent turn:
        # bound the call with a timeout and return an error observation instead.
        try:
            result = await asyncio.wait_for(
                self._session.call_tool(self.tool_name, arguments),
                timeout=MCP_CALL_TIMEOUT,
            )
        except (TimeoutError, asyncio.TimeoutError):
            return ToolResult(
                tool_call_id="",
                content=f"MCP tool {self.tool_name!r} timed out after {MCP_CALL_TIMEOUT:.0f}s.",
                is_error=True,
            )
        except Exception as exc:
            return ToolResult(
                tool_call_id="",
                content=f"MCP tool {self.tool_name!r} failed: {type(exc).__name__}: {exc}",
                is_error=True,
            )
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

    @classmethod
    async def from_paths(
        cls, paths: list[str], allowed_servers: list[str] | None = None
    ) -> "McpClientManager":
        """Load and merge several config files (project + global) into one manager.

        When ``allowed_servers`` is given, only servers whose name is in the list
        are connected — filtering happens *before* connecting so excluded servers
        never launch a subprocess or open a socket.
        """
        manager = cls()
        servers = load_and_merge_mcp_configs(paths)
        if allowed_servers is not None:
            allow = set(allowed_servers)
            servers = [s for s in servers if s.name in allow]
        await manager.start(servers)
        return manager

    async def start(self, servers: list[McpServerConfig]) -> None:
        """Connect to each configured server. A failing server is logged and skipped
        so one broken server cannot take down the others."""
        if self._started:
            return
        for server in servers:
            try:
                await self._start_server(server)
            except Exception as exc:
                logger.warning(
                    "MCP server %s failed to start (%s: %s); skipping it",
                    server.name,
                    type(exc).__name__,
                    exc,
                )
            except asyncio.CancelledError:
                # HTTP/SSE transports run their own anyio task group; a failed
                # connect can surface as CancelledError from that *inner* scope.
                # If our own task wasn't actually cancelled, isolate this server
                # like any other failure instead of aborting every server. If we
                # were genuinely cancelled (e.g. Ctrl-C), re-raise.
                task = asyncio.current_task()
                if task is not None and task.cancelling() > 0:
                    raise
                logger.warning(
                    "MCP server %s failed to start (cancelled during connect); skipping it",
                    server.name,
                )
        self._started = True

    async def _open_transport(self, server: McpServerConfig, server_stack: AsyncExitStack):
        """Open the configured transport and return its ``(read, write)`` streams.

        stdio launches a subprocess; ``http`` (streamable-HTTP) and ``sse`` connect
        to a remote ``url`` with optional ``headers`` (e.g. bearer auth). An unknown
        transport raises so the caller logs and skips the server rather than hanging.
        """
        transport = normalize_transport(server.transport, has_url=bool(server.url))
        if transport == "stdio":
            params = StdioServerParameters(
                command=server.command,
                args=server.args,
                env=server.env or None,
            )
            read, write = await server_stack.enter_async_context(stdio_client(params))
            return read, write
        if transport in ("http", "sse"):
            if not server.url:
                raise ValueError(
                    f"MCP server {server.name!r} uses transport={transport} but has no url"
                )
            headers = server.headers or None
            if transport == "http":
                ctx = streamablehttp_client(
                    server.url, headers=headers, timeout=MCP_HTTP_CONNECT_TIMEOUT
                )
                # streamable-HTTP yields a third element (a session-id getter) we don't need.
                read, write, *_ = await server_stack.enter_async_context(ctx)
                return read, write
            ctx = sse_client(server.url, headers=headers, timeout=MCP_HTTP_CONNECT_TIMEOUT)
            read, write = await server_stack.enter_async_context(ctx)
            return read, write
        raise ValueError(f"Unknown MCP transport {transport!r} for server {server.name!r}")

    async def _start_server(self, server: McpServerConfig) -> None:
        # Per-server exit stack: on failure only this server's resources are
        # torn down; on success ownership moves to the manager's stack.
        server_stack = AsyncExitStack()
        try:
            read, write = await self._open_transport(server, server_stack)
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
        existing = {t.name for t in self._tools}
        for tool in listed.tools:
            schema = tool.inputSchema if hasattr(tool, "inputSchema") else {"type": "object", "properties": {}}
            remote = McpRemoteTool(
                server_name=server.name,
                tool_name=tool.name,
                description=tool.description or tool.name,
                parameters=schema,
                _session=session,
            )
            # Two long names can sanitize/truncate to the same exposed name; suffix
            # collisions so a later tool never silently shadows an earlier one.
            if remote.name in existing:
                suffix = f"_{len(self._tools)}"
                remote.name_override = remote.name[: MCP_TOOL_NAME_MAX_LEN - len(suffix)] + suffix
                logger.warning(
                    "MCP tool name collision for %s; exposing as %s",
                    remote.name,
                    remote.name_override,
                )
            existing.add(remote.name)
            self._tools.append(remote)

    def get_tools(self) -> list[Tool]:
        return list(self._tools)

    async def close(self) -> None:
        await self._stack.aclose()
        self._started = False
        self._tools = []
