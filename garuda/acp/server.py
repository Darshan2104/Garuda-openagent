"""Garuda as an ACP server (P2.1, issue #47).

ACP-native editors spawn `python -m garuda.acp.server` and speak the owned
wire subset: initialize, session/new, session/prompt, session/cancel,
session/approve. Sessions run behind the same `AgentRuntime` boundary every
other surface uses — the server drives runtimes, never a second agent loop.
The product driver is the native Garuda runtime (`--driver echo` exists for
conformance and smoke tests only).

Isolation is structural: each server instance holds its own session table and
shares nothing with the outbound client (`AcpProcess`) — no globals, no shared
mutable state. Transport is stdio only: this module opens no socket, and any
future listener needs explicit secure configuration to exist at all.

Dispatch is concurrent with a bound: prompts run as tasks so a cancel or
approval arriving mid-turn is processed instead of waiting behind the blocked
prompt; everything else dispatches inline, and the task set is bounded with
backpressure. A valid initialize handshake is required before any session
method; malformed framing ends the process nonzero without leaking internals.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from garuda.acp.protocol import (
    ACP_VERSION,
    AcpCancelledError,
    AcpError,
    AcpProtocolError,
    decode_frame,
    encode_frame,
)

logger = logging.getLogger(__name__)

#: Ceiling on concurrent prompt tasks. Past it the reader waits for one to
#: finish instead of growing the set without bound.
MAX_INFLIGHT_TASKS = 64
CLEANUP_TIMEOUT = 5.0

MakeRuntime = Callable[[str], Awaitable[Any]]


class AcpServer:
    """One stdio ACP server. `send` sinks framed replies and notifications."""

    def __init__(
        self,
        make_runtime: MakeRuntime,
        send: Callable[[bytes], Awaitable[None]],
    ):
        self._make_runtime = make_runtime
        self._send = send
        self._sessions: dict[str, Any] = {}
        self._initialized = False
        self._next_client_request_id = 0
        self._client_requests: dict[int, asyncio.Future] = {}

    async def _reply(self, call_id: Any, result: Any) -> None:
        await self._send(encode_frame({"jsonrpc": "2.0", "id": call_id, "result": result}))

    async def _fail(self, call_id: Any, message: str) -> None:
        await self._send(
            encode_frame(
                {
                    "jsonrpc": "2.0",
                    "id": call_id,
                    "error": {"code": -32000, "message": message},
                }
            )
        )

    async def _notify(self, update: dict[str, Any]) -> None:
        await self._send(
            encode_frame({"jsonrpc": "2.0", "method": "session/update", "params": {"update": update}})
        )

    async def _request_client(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self._next_client_request_id += 1
        request_id = self._next_client_request_id
        future = asyncio.get_running_loop().create_future()
        self._client_requests[request_id] = future
        await self._send(encode_frame({
            "jsonrpc": "2.0", "id": request_id, "method": method, "params": params,
        }))
        try:
            result = await future
            return result if isinstance(result, dict) else {}
        finally:
            self._client_requests.pop(request_id, None)

    async def handle(self, message: dict[str, Any]) -> None:
        """Dispatch one decoded frame. Unknown methods fail closed, never hang."""
        if not isinstance(message, dict):
            raise AcpProtocolError("message must be an object")
        if "method" not in message and "id" in message:
            future = self._client_requests.get(message["id"])
            if future is None:
                raise AcpProtocolError("response does not match a pending request")
            if "error" in message:
                future.set_exception(AcpProtocolError("client request was refused"))
            else:
                future.set_result(message.get("result", {}))
            return
        method = message.get("method", "")
        call_id = message.get("id")
        params = message.get("params", {})
        if not isinstance(params, dict):
            params = {}
        handler = {
            "initialize": self._on_initialize,
            "session/new": self._on_session_new,
            "session/prompt": self._on_prompt,
            "session/cancel": self._on_cancel,
            "session/approve": self._on_approve,
        }.get(method)
        if handler is None:
            if call_id is not None:
                await self._fail(call_id, f"unsupported method {method!r}")
            return
        try:
            await handler(call_id, params)
        except AcpError as exc:
            if call_id is not None:
                await self._fail(call_id, str(exc))
        except Exception:
            logger.warning("ACP server handler failed", exc_info=True)
            if call_id is not None:
                await self._fail(call_id, "internal error")

    async def _on_initialize(self, call_id: Any, params: dict) -> None:
        client_version = params.get("protocolVersion")
        if client_version != ACP_VERSION:
            raise AcpProtocolError(
                f"client speaks ACP {client_version!r}, server requires {ACP_VERSION!r}"
            )
        self._initialized = True
        await self._reply(
            call_id,
            {
                "protocolVersion": ACP_VERSION,
                "agentCapabilities": {
                    "families": ["edit", "terminal", "mcp", "approval"],
                    "mediated": ["edit", "terminal", "mcp", "approval"],
                    "sandbox": False,
                },
            },
        )

    async def _on_session_new(self, call_id: Any, params: dict) -> None:
        if not self._initialized:
            raise AcpProtocolError("initialize first: no handshake completed")
        session_id = f"garuda-{uuid.uuid4().hex[:12]}"
        runtime = await self._make_runtime(session_id)
        await runtime.start(task="acp session", session_id=session_id)
        self._sessions[session_id] = runtime
        await self._reply(call_id, {"sessionId": session_id})

    def _session(self, params: dict) -> Any:
        if not self._initialized:
            raise AcpProtocolError("initialize first: no handshake completed")
        session_id = params.get("sessionId", "")
        runtime = self._sessions.get(session_id)
        if runtime is None:
            raise AcpProtocolError(f"unknown session {session_id!r}")
        return runtime

    async def _on_prompt(self, call_id: Any, params: dict) -> None:
        runtime = self._session(params)
        prompt = params.get("prompt")
        if not isinstance(prompt, list) or not prompt:
            raise AcpProtocolError("session/prompt needs content blocks")
        first = prompt[0]
        text = first.get("text") if isinstance(first, dict) and first.get("type") == "text" else None
        if not isinstance(text, str) or not text:
            raise AcpProtocolError("session/prompt needs a non-empty text block")
        before, _ = await runtime.poll_events(0)
        seen = len(before)
        prompt_task = asyncio.ensure_future(runtime.prompt(text))
        try:
            while not prompt_task.done():
                events, seen = await runtime.poll_events(seen)
                for event in events:
                    await self._deliver_event(runtime, event)
                await asyncio.sleep(0.01)
            await prompt_task
            events, seen = await runtime.poll_events(seen)
            for event in events:
                await self._deliver_event(runtime, event)
        except AcpCancelledError:
            await self._notify({"sessionUpdate": "error", "message": "cancelled by client"})
            await self._reply(call_id, {"stopReason": "cancelled"})
            return
        await self._reply(call_id, {"stopReason": "end_turn"})

    async def _deliver_event(self, runtime: Any, event: Any) -> None:
        if event.kind.value == "approval_request":
            result = await self._request_client(
                "session/request_permission",
                {
                    "sessionId": event.session_id,
                    "approvalId": event.payload.get("approval_id", ""),
                    "action": event.payload.get("action", ""),
                },
            )
            await runtime.permission_response(
                approval_id=event.payload.get("approval_id", ""),
                allow=bool(result.get("approved", False)),
            )
            return
        update = _to_update(event)
        if update is not None:
            await self._notify(update)

    async def _on_cancel(self, call_id: Any, params: dict) -> None:
        runtime = self._session(params)
        await runtime.cancel(reason="client cancel")
        if call_id is not None:
            await self._reply(call_id, {"cancelled": True})

    async def _on_approve(self, call_id: Any, params: dict) -> None:
        runtime = self._session(params)
        approval_id = params.get("approvalId", "")
        if not approval_id:
            raise AcpProtocolError("session/approve needs an approvalId")
        await runtime.permission_response(
            approval_id=approval_id, allow=bool(params.get("approved", False))
        )
        if call_id is not None:
            await self._reply(call_id, {"approved": bool(params.get("approved", False))})

    def session_count(self) -> int:
        return len(self._sessions)


def _to_update(event: Any) -> dict[str, Any] | None:
    """Project one normalized event onto a wire update. Lifecycle bookkeeping
    stays server-side; the client sees content, calls, diffs, and approvals."""
    kind = event.kind.value
    payload = event.payload
    if kind == "message":
        text = payload.get("chunk", payload.get("text", ""))
        return {
            "sessionUpdate": "agent_message_chunk",
            "content": {"type": "text", "text": text},
        } if text else None
    if kind == "tool_call":
        return {
            "sessionUpdate": "tool_call",
            "toolCallId": payload.get("tool_call_id", ""),
            "title": payload.get("title", payload.get("tool", "")),
        }
    if kind == "tool_result":
        if payload.get("status") == "diff":
            return {
                "sessionUpdate": "diff",
                "path": payload.get("path", ""),
                "oldText": payload.get("old_text", ""),
                "newText": payload.get("new_text", ""),
            }
        return {
            "sessionUpdate": "tool_call_update",
            "toolCallId": payload.get("tool_call_id", ""),
            "status": payload.get("status", ""),
        }
    if kind == "approval_request":
        return {
            "sessionUpdate": "approval_request",
            "approvalId": payload.get("approval_id", ""),
            "action": payload.get("action", ""),
        }
    if kind == "error":
        return {"sessionUpdate": "error", "message": payload.get("message", "")}
    return None


async def serve_stdio(make_runtime: MakeRuntime) -> int:
    """Run the framing loop over real stdin/stdout. Returns a process exit code.

    Prompts dispatch as bounded background tasks so a cancel or approval
    arriving mid-turn is handled instead of waiting behind the blocked
    prompt; every other message dispatches inline, preserving arrival order
    for the fast paths. Malformed framing ends the process nonzero (2)
    without leaking internals; transport errors end it nonzero (1).
    """
    loop = asyncio.get_running_loop()
    outbox: asyncio.Queue[bytes] = asyncio.Queue()

    async def _send(frame: bytes) -> None:
        outbox.put_nowait(frame)

    async def _writer() -> None:
        while True:
            await loop.run_in_executor(None, _write_stdout, await outbox.get())

    server = AcpServer(make_runtime, _send)
    writer = asyncio.ensure_future(_writer())
    pending: set[asyncio.Task[None]] = set()

    def _reap(task: asyncio.Task[None]) -> None:
        pending.discard(task)
        if not task.cancelled():
            exc = task.exception()
            if exc is not None:
                logger.warning("ACP server prompt task failed", exc_info=exc)

    async def _dispatch(message: dict[str, Any]) -> None:
        if message.get("method") == "session/prompt":
            while len(pending) >= MAX_INFLIGHT_TASKS:
                await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            task = asyncio.ensure_future(server.handle(message))
            pending.add(task)
            task.add_done_callback(_reap)
        else:
            await server.handle(message)

    buffer = b""
    exit_code = 0
    try:
        while True:
            chunk = await loop.run_in_executor(None, sys.stdin.buffer.read1, 65536)
            if not chunk:
                break
            buffer += chunk
            while True:
                try:
                    message, buffer = decode_frame(buffer)
                except ValueError:
                    break
                await _dispatch(message)
            if buffer.strip():
                raise AcpProtocolError("incomplete NDJSON record")
    except AcpProtocolError:
        print("error: malformed input frame", file=sys.stderr)
        exit_code = 2
    except OSError:
        exit_code = 1
    finally:
        if pending:
            _done, waiting = await asyncio.wait(pending, timeout=CLEANUP_TIMEOUT)
            for task in waiting:
                task.cancel()
            await asyncio.gather(*waiting, return_exceptions=True)
        writer.cancel()
        await asyncio.gather(writer, return_exceptions=True)
        for session in list(server._sessions.values()):
            try:
                await asyncio.wait_for(session.close(), CLEANUP_TIMEOUT)
            except Exception:
                pass
    return exit_code


def _write_stdout(frame: bytes) -> None:
    sys.stdout.buffer.write(frame)
    sys.stdout.buffer.flush()


async def make_echo_runtime(session_id: str) -> Any:
    """Deterministic driver for conformance and smoke tests. Not for real work."""
    from garuda.runtime.fake import FakeRuntime, FakeScenario

    return FakeRuntime(FakeScenario.SUCCESS, runtime_id="garuda-echo")


async def make_native_runtime(session_id: str, *, workspace: str = ".") -> Any:
    """Production driver: the native loop behind the runtime boundary."""
    from garuda.agents.setup import prepare_agent_run
    from garuda.core.events import EventStore
    from garuda.interfaces.runner import resolve_environment
    from garuda.runtime.native import NativeGarudaRuntime

    prepared = await prepare_agent_run("build", workspace=workspace)
    profile, config, permissions, tools, agent, mcp_manager = prepared
    model = prepared.reasoning
    runtime = NativeGarudaRuntime(
        agent=agent, model=model, tools=tools, config=config,
        permissions=permissions, resource_manager=mcp_manager,
    )

    async def _driver(*, task: str, turn: int, trail: EventStore) -> Any:
        env, handle = await resolve_environment("local", workspace, "ubuntu:22.04")
        try:
            return await agent.run(
                task=task, model=model, env=env, tools=tools, config=config,
                events=trail, permissions=permissions,
            )
        finally:
            try:
                await handle.stop()
            except Exception:
                pass

    runtime.install_driver(_driver)
    return runtime


def _cli_parser() -> Any:
    import argparse

    parser = argparse.ArgumentParser(description="Garuda as an ACP server (stdio only).")
    parser.add_argument(
        "--driver", choices=("echo", "native"), default="native",
        help="echo is a test double; native runs the real Garuda loop.",
    )
    parser.add_argument("--workspace", default=".")
    parser.add_argument(
        "--listen",
        default=None,
        help="Refused: this server is stdio-only. No network listener exists.",
    )
    return parser


def _select_driver(name: str, workspace: str) -> MakeRuntime:
    if name == "native":
        import functools

        return functools.partial(make_native_runtime, workspace=workspace)
    return make_echo_runtime


def main(argv: list[str] | None = None) -> int:
    args = _cli_parser().parse_args(argv)
    if args.listen is not None:
        print(
            "error: network listeners are not supported; "
            "run behind stdio only (explicit secure configuration required).",
            file=sys.stderr,
        )
        return 2
    return asyncio.run(serve_stdio(_select_driver(args.driver, args.workspace)))


if __name__ == "__main__":
    raise SystemExit(main())
