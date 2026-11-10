import asyncio
import json
import logging
import re
import weakref
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

import anyio
from mcp import ClientSession, StdioServerParameters, types
from mcp.client.sse import sse_client
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamablehttp_client

from garuda.mcp.config import (
    McpServerConfig,
    load_mcp_config,
    normalize_transport,
    partition_mcp_servers,
)
from garuda.mcp.content import MCP_CONTEXT_ITEMS, context_result
from garuda.model.config import ConfigError
from garuda.tools.protocol import Tool, ToolContext, ToolEffect
from garuda.types import ToolResult

logger = logging.getLogger(__name__)

# Timeout (seconds) for each server's initialize() and list_tools() handshakes.
MCP_HANDSHAKE_TIMEOUT = 10.0

# Connect/handshake timeout (seconds) for HTTP/SSE transports.
MCP_HTTP_CONNECT_TIMEOUT = 30.0

# Timeout (seconds) for a single MCP tool invocation.
MCP_CALL_TIMEOUT = 120.0

# Startup barrier (seconds): every server is launched at once, and one not ready
# by then is stopped. A required one refuses the run; an optional one is skipped.
MCP_STARTUP_DEADLINE = 60.0

# Reconnect bounds after a stdio server's process exits: relaunches per run (a
# server that handshakes and exits again spends the same budget), and the first
# backoff delay (doubled each relaunch, capped).
MCP_RECONNECT_ATTEMPTS = 3
MCP_RECONNECT_BACKOFF = 0.5
MCP_RECONNECT_BACKOFF_MAX = 8.0

# How long close() waits for a server's owner task before cancelling it.
MCP_CLOSE_TIMEOUT = 10.0

# Maximum length of an exposed MCP tool name.
MCP_TOOL_NAME_MAX_LEN = 64

_TOOL_NAME_SAFE = re.compile(r"[^a-zA-Z0-9_-]")


def sanitize_tool_name(raw: str) -> str:
    """Replace characters outside [a-zA-Z0-9_-] with _ and cap the length at 64."""
    return _TOOL_NAME_SAFE.sub("_", raw)[:MCP_TOOL_NAME_MAX_LEN]


@dataclass(eq=False)
class McpRemoteTool:
    server_name: str
    tool_name: str
    description: str
    parameters: dict[str, Any]
    _session: ClientSession
    name_override: str | None = None
    effect: ToolEffect = ToolEffect.UNKNOWN
    # The connection owner when a manager created this tool: calls go to its
    # current session, and fail fast while it is not connected.
    _owner: "_ServerOwner | None" = field(default=None, repr=False)
    _context_access: bool = field(default=False, repr=False)

    @property
    def effect_trusted(self) -> bool:
        """Whether the manager sourced this effect from global authority."""
        return self in _TRUSTED_EFFECT_TOOLS

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
        if self._context_access:
            if (ctx is not None and ctx.allowed_tool_effects is not None
                    and self.effect not in ctx.allowed_tool_effects):
                return ToolResult(tool_call_id="", content="MCP context read effect denied.", is_error=True)
            return await self._owner.read_context(arguments, ctx)
        session = self._session
        if self._owner is not None:
            session = self._owner.session_for(self.tool_name)
            if session is None:
                # Never queued for a later connection: a call is sent at most once.
                return ToolResult(
                    tool_call_id="",
                    content=self._owner.unavailable_message(self.tool_name),
                    is_error=True,
                )
        # A hung or failing MCP server must not hang or crash the agent turn:
        # bound the call with a timeout and return an error observation instead.
        try:
            result = await asyncio.wait_for(
                session.call_tool(self.tool_name, arguments),
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


# Identity-owned provenance: a project tool can copy a name or effect value but
# cannot become trusted merely by constructing McpRemoteTool or subclassing it.
# This remains a guardrail inside one Python process, not a sandbox boundary.
_TRUSTED_EFFECT_TOOLS: weakref.WeakSet[McpRemoteTool] = weakref.WeakSet()


def _trust_remote_effect(tool: McpRemoteTool) -> None:
    _TRUSTED_EFFECT_TOOLS.add(tool)


class McpTrustError(ConfigError):
    """A run requires a project MCP server the user has not trusted."""


class McpStartupError(ConfigError):
    """A required MCP server was not ready by the startup barrier."""

    def __init__(self, message: str, states: list[dict[str, Any]]):
        super().__init__(message)
        #: Per-server status at the barrier, for callers that show what failed.
        self.states = states


class McpServerState(str, Enum):
    STARTING = "starting"
    READY = "ready"
    FAILED = "failed"
    RECONNECTING = "reconnecting"
    CLOSED = "closed"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _describe_failure(exc: BaseException) -> str:
    """A bounded, redacted reason; transport errors can echo URLs or arguments."""
    from garuda.context.redact import redact_text

    if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
        text = "timed out"
    elif isinstance(exc, asyncio.CancelledError):
        text = "cancelled during connect"
    else:
        text = f"{type(exc).__name__}: {exc}"
    return redact_text(text)[0][:300]


class _ServerOwner:
    """One server's connection, held by a single task from launch to teardown."""

    def __init__(self, config: McpServerConfig):
        self.config = config
        self.state = McpServerState.STARTING
        self.reason = ""
        self.session: ClientSession | None = None
        self.capabilities: Any = None
        # Tools the live connection lists, by remote name, and the ones this run
        # admitted at startup. Only admitted tools are ever exposed.
        self.listed: dict[str, Any] = {}
        self.admitted: list[McpRemoteTool] = []
        self.reconnects = 0
        self.started_at = _now()
        self.ready_at: str | None = None
        self.changed_at = self.started_at
        self.settled = asyncio.Event()
        self.stop = asyncio.Event()
        self.task: asyncio.Task | None = None

    def transition(self, state: McpServerState, reason: str = "") -> None:
        self.state = state
        self.reason = reason
        self.changed_at = _now()
        if state is McpServerState.READY:
            self.ready_at = self.changed_at
        if state is not McpServerState.STARTING:
            self.settled.set()

    def session_for(self, tool_name: str) -> ClientSession | None:
        if self.state is McpServerState.READY and tool_name in self.listed:
            return self.session
        return None

    def unavailable_message(self, tool_name: str) -> str:
        name = self.config.name
        if self.state is McpServerState.READY:
            if tool_name in self.listed:
                # Between a lost connection and the owner recording it.
                return f"MCP server {name!r} lost its connection; tool {tool_name!r} was not called."
            return (
                f"MCP tool {tool_name!r} is no longer offered by server {name!r} "
                "after it reconnected; it was not called."
            )
        detail = f" ({self.reason})" if self.reason else ""
        return (
            f"MCP server {name!r} is {self.state.value}{detail}; "
            f"tool {tool_name!r} was not called."
        )

    def status(self) -> dict[str, Any]:
        return {
            "name": self.config.name,
            "transport": normalize_transport(self.config.transport, has_url=bool(self.config.url)),
            "required": self.config.required,
            "state": self.state.value,
            "reason": self.reason,
            "tools": len(self.admitted),
            "reconnects": self.reconnects,
            "started_at": self.started_at,
            "ready_at": self.ready_at,
            "changed_at": self.changed_at,
        }

    async def read_context(self, arguments: dict[str, Any], ctx: ToolContext | None) -> ToolResult:
        session = self.session
        connection = self.ready_at
        action = arguments.get("action")
        operations = {
            "list_resources": "resources", "read_resource": "resources",
            "list_prompts": "prompts", "get_prompt": "prompts",
        }
        try:
            if self.state is not McpServerState.READY:
                raise ValueError(f"server is {self.state.value}; request was not sent")
            if session is None or self.stop.is_set():
                raise ValueError("server connection is unavailable or closing; request was not sent")
            kind = operations.get(action) if isinstance(action, str) else None
            if kind is None or kind not in self.config.context_access:
                raise ValueError("MCP context operation is not authorized")
            if getattr(self.capabilities, kind, None) is None:
                raise ValueError(f"{kind} unsupported: server capability absent")

            async def request():
                if kind == "resources":
                    listed = await session.list_resources()
                    items = listed.resources[:MCP_CONTEXT_ITEMS]
                    catalog = [{"uri": str(item.uri), "name": item.name,
                                "description": item.description, "mimeType": item.mimeType}
                               for item in items]
                else:
                    listed = await session.list_prompts()
                    items = listed.prompts[:MCP_CONTEXT_ITEMS]
                    catalog = [{"name": item.name, "description": item.description,
                                "arguments": [{"name": arg.name, "required": arg.required,
                                               "description": arg.description}
                                              for arg in (item.arguments or [])[:32]]}
                               for item in items]
                partial = bool(listed.nextCursor) or len(getattr(listed, kind)) > len(items)
                if kind == "prompts" and any(len(item.arguments or []) > 32 for item in items):
                    partial = True
                if self.session is not session or self.state is not McpServerState.READY or self.stop.is_set():
                    raise ValueError("connection lost during listing; request was not replayed")
                if action.startswith("list_"):
                    return {kind: catalog}, partial
                key = "uri" if kind == "resources" else "name"
                identifier = arguments.get(key)
                if not isinstance(identifier, str) or not 0 < len(identifier) <= 2048:
                    raise ValueError(f"{key} must be a nonempty string of at most 2048 characters")
                matches = [item for item in items if str(getattr(item, key)) == identifier]
                if not matches:
                    raise ValueError(f"{key} is outside the bounded server listing; access refused")
                if len(matches) != 1:
                    raise ValueError(f"ambiguous {key} in server listing; access refused")
                selected = matches[0]
                if kind == "resources":
                    result = await session.read_resource(selected.uri)
                    blocks = result.contents
                    content = []
                    for block in blocks[:MCP_CONTEXT_ITEMS]:
                        if str(block.uri) != identifier:
                            raise ValueError("resource result URI differs from the requested URI")
                        text = getattr(block, "text", None)
                        if not isinstance(text, str):
                            partial = True
                            continue
                        content.append({"uri": identifier, "mimeType": block.mimeType, "text": text})
                else:
                    args = arguments.get("arguments", {})
                    if len(selected.arguments or []) > 32:
                        raise ValueError("prompt schema exceeds the 32-argument limit")
                    declared = {arg.name: arg for arg in selected.arguments or []}
                    if (len(declared) != len(selected.arguments or [])
                            or not isinstance(args, dict) or len(args) > 32
                            or any(not isinstance(k, str) or k not in declared
                                   or not isinstance(v, str) or len(v) > 4096 for k, v in args.items())
                            or any(arg.required and arg.name not in args for arg in declared.values())):
                        raise ValueError("prompt arguments must match the listed schema (32 strings, 4096 chars each)")
                    result = await session.get_prompt(identifier, args)
                    blocks = result.messages
                    content = []
                    for message in blocks[:MCP_CONTEXT_ITEMS]:
                        block = message.content
                        if getattr(block, "type", None) != "text":
                            partial = True
                            continue
                        content.append({"role": message.role, "text": block.text})
                return {key: identifier, "content": content}, partial or len(blocks) > MCP_CONTEXT_ITEMS

            data, partial = await asyncio.wait_for(request(), MCP_CALL_TIMEOUT)
            if (self.session is not session or self.state is not McpServerState.READY
                    or connection != self.ready_at or self.stop.is_set()):
                raise ValueError("connection changed; result discarded without replay")
            return context_result(data, {
                "server": self.config.name, "connection_ready_at": connection,
                "session_id": ctx.session_id if ctx is not None else None,
                "trust_scope": "project_grant" if self.config.project_scoped else "user_config",
                "operation": action,
            }, partial=partial)
        except Exception as exc:
            return ToolResult(tool_call_id="", is_error=True,
                              content=f"MCP context request failed (untrusted server data): {_describe_failure(exc)}")


async def _first_of(*events: asyncio.Event) -> None:
    waiters = [asyncio.ensure_future(event.wait()) for event in events]
    try:
        await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for waiter in waiters:
            waiter.cancel()
        await asyncio.gather(*waiters, return_exceptions=True)


async def _watch_stream(source, stack: AsyncExitStack, lost: asyncio.Event):
    """Forward a transport's read stream, setting ``lost`` when it ends by itself.

    A stdio server's stream ends exactly when the server closes its stdout,
    normally because the process exited: the one connection loss Garuda can
    prove. Teardown on our side cancels the pump instead, which is not a loss.
    """
    sink, forwarded = anyio.create_memory_object_stream(0)

    async def pump() -> None:
        try:
            async with sink:
                async for item in source:
                    await sink.send(item)
        except (anyio.ClosedResourceError, anyio.BrokenResourceError):
            return
        lost.set()

    group = await stack.enter_async_context(anyio.create_task_group())
    stack.callback(group.cancel_scope.cancel)
    group.start_soon(pump)
    return forwarded


async def _close_quietly(stack: AsyncExitStack, name: str) -> None:
    try:
        await stack.aclose()
    except Exception:
        # anyio can complain when tearing down a half-initialized or already
        # dead transport; the launch failure or loss is what matters.
        logger.debug("Error closing MCP server %s", name, exc_info=True)


class McpClientManager:
    def __init__(self, *, workspace: str | None = None):
        self._owners: list[_ServerOwner] = []
        self._tools: list[McpRemoteTool] = []
        self._started = False
        # Re-checked before a project server is relaunched after a loss.
        self._workspace = workspace

    @classmethod
    async def from_config(cls, path: str) -> "McpClientManager":
        manager = cls()
        await manager.start(load_mcp_config(path))
        return manager

    @classmethod
    async def from_paths(
        cls,
        paths: list[str],
        allowed_servers: list[str] | None = None,
        *,
        workspace: str | None = None,
        user_paths: list[str] | tuple[str, ...] = (),
    ) -> "McpClientManager":
        """Load and merge several config files (project + global) into one manager.

        When ``allowed_servers`` is given, only servers whose name is in the list
        are connected — filtering happens *before* connecting so excluded servers
        never launch a subprocess or open a socket.

        With ``workspace``, a server defined by the project's own config needs a
        user trust grant (``garuda mcp trust``). An untrusted one is skipped with
        a warning before anything starts; one that ``allowed_servers`` names
        explicitly, or that is marked ``required``, refuses the run instead.
        """
        manager = cls(workspace=workspace)
        await manager.start(cls.resolve_configs(
            paths, allowed_servers, workspace=workspace, user_paths=user_paths))
        return manager

    @staticmethod
    def resolve_configs(
        paths: list[str], allowed_servers: list[str] | None = None, *,
        workspace: str | None = None, user_paths: list[str] | tuple[str, ...] = (),
    ) -> list[McpServerConfig]:
        """Resolve trust and filtering without launching or connecting any server."""
        from garuda.mcp.trust import UNTRUSTED_CODE, describe

        servers, untrusted = partition_mcp_servers(
            paths, workspace=workspace, user_paths=user_paths
        )
        allow = set(allowed_servers) if allowed_servers is not None else None
        required = sorted({s.name for s in untrusted if allow is not None and s.name in allow})
        # A server its entry marks required, and the profile does not exclude, is
        # needed by the run as well: refusing beats going on without it.
        required += sorted({
            s.name for s in untrusted
            if s.required and (allow is None or s.name in allow) and s.name not in required
        })
        if required:
            raise McpTrustError(
                f"{UNTRUSTED_CODE}: the profile requires project MCP server(s) "
                f"{', '.join(required)}, which you have not trusted for this repository. "
                f"Review them and run `garuda mcp trust --workspace {workspace}`."
            )
        for server in untrusted:
            logger.warning(
                "%s: skipped project MCP server %r from %s (would %s). Review it and "
                "run `garuda mcp trust --workspace %s %s` to allow it.",
                UNTRUSTED_CODE,
                server.name,
                server.source_path,
                describe(server),
                workspace,
                server.name,
            )
        if allow is not None:
            servers = [s for s in servers if s.name in allow]
        return servers

    async def start(self, servers: list[McpServerConfig]) -> None:
        """Launch every server at once behind one bounded startup barrier.

        Each server is owned by its own task from launch to teardown. One not
        ready by ``MCP_STARTUP_DEADLINE`` is stopped, so it can never register
        tools after the run's tool list is fixed. An optional failure is logged,
        skipped and kept in :meth:`server_states`; a required one closes every
        server and raises :class:`McpStartupError` before any prompt work.
        """
        if self._started:
            return
        self._started = True
        self._owners = [_ServerOwner(server) for server in servers]
        for owner in self._owners:
            owner.task = asyncio.create_task(
                self._own(owner), name=f"mcp-server:{owner.config.name}"
            )
        try:
            await self._barrier(self._owners)
        except BaseException:
            # Cancelled while waiting (e.g. Ctrl-C): nothing started may outlive it.
            await self.close()
            raise
        missing = [
            owner for owner in self._owners
            if owner.config.required and owner.state is not McpServerState.READY
        ]
        if missing:
            states = self.server_states()
            await self.close()
            detail = "; ".join(
                f"{o.config.name} ({o.state.value}: {o.reason or 'no reason recorded'})"
                for o in missing
            )
            raise McpStartupError(
                f"required MCP server(s) not ready: {detail}. Fix the server, or mark it "
                "`required: false` to let runs continue without it.",
                states,
            )
        # Admitted in configuration order, so exposed names stay deterministic.
        for owner in self._owners:
            if owner.state is McpServerState.READY:
                self._admit(owner)

    async def _barrier(self, owners: list[_ServerOwner]) -> None:
        deadline = MCP_STARTUP_DEADLINE
        try:
            await asyncio.wait_for(
                asyncio.gather(*(owner.settled.wait() for owner in owners)), deadline
            )
        except (TimeoutError, asyncio.TimeoutError):
            pass
        late = [owner for owner in owners if not owner.settled.is_set()]
        for owner in late:
            # Stopped before any await, so a handshake finishing in the same tick
            # sees the verdict and cannot report the owner ready.
            owner.stop.set()
            owner.transition(McpServerState.FAILED, f"not ready within {deadline:g}s")
            logger.warning(
                "MCP server %s was not ready within %gs; stopping it", owner.config.name, deadline
            )
        await asyncio.gather(*(self._stop(owner) for owner in late))

    async def _open_transport(
        self,
        server: McpServerConfig,
        server_stack: AsyncExitStack,
        *,
        lost: asyncio.Event | None = None,
    ):
        """Open the configured transport and return its ``(read, write)`` streams.

        stdio launches a subprocess; ``http`` (streamable-HTTP) and ``sse`` connect
        to a remote ``url`` with optional ``headers`` (e.g. bearer auth). An unknown
        transport raises so the caller logs and skips the server rather than hanging.
        With ``lost``, a stdio stream that ends by itself sets it (see
        ``_watch_stream``); remote transports report no proved loss.
        """
        transport = normalize_transport(server.transport, has_url=bool(server.url))
        if transport == "stdio":
            params = StdioServerParameters(
                command=server.command,
                args=server.args,
                env=server.env or None,
            )
            read, write = await server_stack.enter_async_context(stdio_client(params))
            if lost is not None:
                read = await _watch_stream(read, server_stack, lost)
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

    async def _connect(self, server: McpServerConfig, stack: AsyncExitStack, lost: asyncio.Event):
        read, write = await self._open_transport(server, stack, lost=lost)
        session = await stack.enter_async_context(ClientSession(read, write))
        initialized = await asyncio.wait_for(session.initialize(), MCP_HANDSHAKE_TIMEOUT)
        capabilities = getattr(initialized, "capabilities", None)
        if server.context_access and capabilities is not None and capabilities.tools is None:
            listed = types.ListToolsResult(tools=[])
        else:
            listed = await asyncio.wait_for(session.list_tools(), MCP_HANDSHAKE_TIMEOUT)
        return session, listed, capabilities

    async def _own(self, owner: _ServerOwner) -> None:
        """Hold one server's connection for its whole life, in this task only.

        Transport contexts are entered and exited by the same task (anyio
        requires it), so teardown never races a half-open connection. A server
        that never became ready is not retried; one whose stdio process exits
        is relaunched at most ``MCP_RECONNECT_ATTEMPTS`` times per run, counting
        relaunches that connected and then exited again. In-flight calls on a
        lost connection fail and are never replayed.
        """
        name = owner.config.name
        relaunches = 0
        while True:
            lost = asyncio.Event()
            stack = AsyncExitStack()
            try:
                session, listed, capabilities = await self._connect(owner.config, stack, lost)
            except BaseException as exc:
                await _close_quietly(stack, name)
                if isinstance(exc, asyncio.CancelledError):
                    # HTTP/SSE transports run their own anyio task group; a failed
                    # connect can surface as CancelledError from that inner scope.
                    # Only a cancellation of this task itself ends the owner.
                    task = asyncio.current_task()
                    if task is not None and task.cancelling() > 0:
                        raise
                elif not isinstance(exc, Exception):
                    raise
                reason = _describe_failure(exc)
                if owner.ready_at is None:
                    logger.warning("MCP server %s failed to start (%s); skipping it", name, reason)
                    owner.transition(McpServerState.FAILED, reason)
                    return
                if relaunches >= MCP_RECONNECT_ATTEMPTS:
                    owner.transition(
                        McpServerState.FAILED,
                        f"reconnect failed after {relaunches} attempt(s): {reason}",
                    )
                    logger.warning("MCP server %s: %s", name, owner.reason)
                    return
                if not await self._backoff(owner, relaunches + 1):
                    return
                relaunches += 1
                continue
            if owner.stop.is_set() or owner.state is McpServerState.FAILED:
                # Stopped (startup verdict or close) while the handshake finished:
                # never report ready, and release the connection here.
                await _close_quietly(stack, name)
                return
            owner.session = session
            owner.capabilities = capabilities
            owner.listed = {tool.name: tool for tool in listed.tools}
            if owner.ready_at is not None:
                owner.reconnects += 1
                self._refresh(owner)
            owner.transition(McpServerState.READY)
            try:
                await _first_of(owner.stop, lost)
            finally:
                owner.session = None
                await _close_quietly(stack, name)
            if owner.stop.is_set():
                return
            refusal = self._reconnect_refusal(owner.config)
            if refusal:
                owner.transition(McpServerState.FAILED, f"connection lost; {refusal}")
                logger.warning("MCP server %s: %s", name, owner.reason)
                return
            if relaunches >= MCP_RECONNECT_ATTEMPTS:
                owner.transition(
                    McpServerState.FAILED,
                    f"connection lost: server process exited after {relaunches} "
                    "relaunch(es) this run; restart the run",
                )
                logger.warning("MCP server %s: %s", name, owner.reason)
                return
            owner.transition(McpServerState.RECONNECTING, "connection lost: server process exited")
            logger.warning("MCP server %s exited; reconnecting", name)
            if not await self._backoff(owner, relaunches + 1):
                return
            relaunches += 1

    async def _backoff(self, owner: _ServerOwner, attempt: int) -> bool:
        """Wait before reconnect ``attempt``; False if the server was stopped meanwhile."""
        owner.transition(McpServerState.RECONNECTING, owner.reason or "reconnecting")
        delay = min(MCP_RECONNECT_BACKOFF * 2 ** (attempt - 1), MCP_RECONNECT_BACKOFF_MAX)
        try:
            await asyncio.wait_for(owner.stop.wait(), delay)
        except (TimeoutError, asyncio.TimeoutError):
            return True
        return False

    def _reconnect_refusal(self, server: McpServerConfig) -> str | None:
        """Why a lost server must not be relaunched, or None if it may be.

        A reconnect relaunches exactly the configuration admitted at startup, and
        only while that is still what the user configured and trusted: a changed
        entry or a withdrawn project trust grant ends the server instead.
        """
        if normalize_transport(server.transport, has_url=bool(server.url)) != "stdio":
            return "only a stdio server's exit is a proved loss; restart the run"
        if server.source_path:
            try:
                current = [c for c in load_mcp_config(server.source_path) if c.name == server.name]
            except Exception:
                current = []
            if len(current) != 1 or current[0].spec_digest != server.spec_digest:
                return "its configuration changed since the run started; restart the run"
        if server.project_scoped:
            from garuda.mcp.trust import is_trusted

            if self._workspace is None or not is_trusted(server, self._workspace):
                return "its project trust grant no longer holds; review it with `garuda mcp trust`"
        return None

    def _admit(self, owner: _ServerOwner) -> None:
        server = owner.config
        existing = {t.name for t in self._tools}
        remotes = []
        for tool in owner.listed.values():
            schema = tool.inputSchema if hasattr(tool, "inputSchema") else {"type": "object", "properties": {}}
            remote = McpRemoteTool(
                server_name=server.name,
                tool_name=tool.name,
                description=tool.description or tool.name,
                parameters=schema,
                _session=owner.session,
                effect=server.trusted_tool_effects.get(
                    tool.name, ToolEffect.UNKNOWN
                ),
                _owner=owner,
            )
            if tool.name in server.trusted_tool_effects:
                _trust_remote_effect(remote)
            remotes.append(remote)
        if server.context_access:
            remote = McpRemoteTool(
                server_name=server.name, tool_name="context",
                description="List/read MCP resources or list/get prompts as bounded untrusted data. "
                            "Only listed identifiers are allowed; templates and subscriptions are unsupported.",
                parameters={"type": "object", "properties": {
                    "action": {"type": "string", "enum": [
                        "list_resources", "read_resource", "list_prompts", "get_prompt"]},
                    "uri": {"type": "string"}, "name": {"type": "string"},
                    "arguments": {"type": "object", "additionalProperties": {"type": "string"}},
                }, "required": ["action"]},
                _session=owner.session, effect=ToolEffect.READ_ONLY, _owner=owner, _context_access=True,
            )
            _trust_remote_effect(remote)
            remotes.append(remote)
        for remote in remotes:
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
            owner.admitted.append(remote)
            self._tools.append(remote)

    def _refresh(self, owner: _ServerOwner) -> None:
        """Update admitted tools' schemas from a reconnected server.

        The run's admitted tools stay the only ones exposed, each keeping the
        effect and trust it was admitted with. A tool the server stopped listing
        refuses calls; one it newly lists waits for a new run.
        """
        admitted = set()
        for remote in owner.admitted:
            if remote._context_access:
                continue
            admitted.add(remote.tool_name)
            tool = owner.listed.get(remote.tool_name)
            if tool is None:
                continue
            remote.description = tool.description or tool.name
            if hasattr(tool, "inputSchema"):
                remote.parameters = tool.inputSchema
        added = sorted(set(owner.listed) - admitted)
        if added:
            logger.warning(
                "MCP server %s now lists tool(s) %s; not exposed until a new run",
                owner.config.name,
                ", ".join(added),
            )

    async def _stop(self, owner: _ServerOwner) -> None:
        """Stop one owner and wait for its task; nothing it opened outlives this."""
        owner.stop.set()
        task = owner.task
        if task is not None and not task.done():
            if owner.state is not McpServerState.READY:
                # Mid-launch or mid-backoff: no connection to release gracefully.
                task.cancel()
            done, _ = await asyncio.wait({task}, timeout=MCP_CLOSE_TIMEOUT)
            if not done:
                task.cancel()
                await asyncio.wait({task})
        if task is not None and not task.cancelled() and task.exception() is not None:
            logger.warning(
                "MCP server %s owner failed", owner.config.name, exc_info=task.exception()
            )

    def get_tools(self) -> list[Tool]:
        return list(self._tools)

    def server_states(self) -> list[dict[str, Any]]:
        """Per-server lifecycle status: state, reason, counts and timestamps.

        A read of what the manager already knows; it never probes a server.
        """
        return [owner.status() for owner in self._owners]

    async def close(self) -> None:
        owners = self._owners
        await asyncio.gather(*(self._stop(owner) for owner in owners))
        for owner in owners:
            if owner.state is not McpServerState.FAILED:
                owner.transition(McpServerState.CLOSED)
        self._started = False
        self._tools = []
