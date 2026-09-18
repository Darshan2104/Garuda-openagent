"""Garuda as an ACP server (P2.1, issue #47).

ACP-native editors spawn `python -m garuda.acp.server` and speak the owned
wire subset: initialize, session/new, session/prompt, session/cancel,
session/approve. Sessions run behind the same `AgentRuntime` boundary every
other surface uses — the server drives runtimes, never a second agent loop.

Isolation is structural: each server instance holds its own session table and
shares nothing with the outbound client (`AcpProcess`) — no globals, no shared
mutable state. Transport is stdio only: this module opens no socket, and any
future listener needs explicit secure configuration to exist at all.
"""

from __future__ import annotations

import asyncio
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

    async def handle(self, message: dict[str, Any]) -> None:
        """Dispatch one decoded frame. Unknown methods fail closed, never hang."""
        if not isinstance(message, dict):
            raise AcpProtocolError("message must be an object")
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
        except Exception as exc:
            if call_id is not None:
                await self._fail(call_id, f"internal error: {exc}")

    async def _on_initialize(self, call_id: Any, params: dict) -> None:
        client_version = (params.get("protocolVersion") or "")
        if client_version and client_version != ACP_VERSION:
            raise AcpProtocolError(
                f"client speaks ACP {client_version!r}, server requires {ACP_VERSION!r}"
            )
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
        session_id = f"garuda-{uuid.uuid4().hex[:12]}"
        runtime = await self._make_runtime(session_id)
        await runtime.start(task=params.get("task", "acp session"), session_id=session_id)
        self._sessions[session_id] = runtime
        await self._reply(call_id, {"sessionId": session_id})

    def _session(self, params: dict) -> Any:
        session_id = params.get("sessionId", "")
        runtime = self._sessions.get(session_id)
        if runtime is None:
            raise AcpProtocolError(f"unknown session {session_id!r}")
        return runtime

    async def _on_prompt(self, call_id: Any, params: dict) -> None:
        runtime = self._session(params)
        text = params.get("prompt", "")
        if not isinstance(text, str) or not text:
            raise AcpProtocolError("session/prompt needs a non-empty prompt")
        before, _ = await runtime.poll_events(0)
        seen = len(before)
        try:
            await runtime.prompt(text)
        except AcpCancelledError:
            await self._notify({"updateType": "error", "message": "cancelled by client"})
            await self._reply(call_id, {"stopReason": "cancelled"})
            return
        events, _ = await runtime.poll_events(seen)
        for event in events:
            update = _to_update(event)
            if update is not None:
                await self._notify(update)
        await self._reply(call_id, {"stopReason": "end_turn"})

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
        return {"updateType": "agent_message_chunk", "text": text} if text else None
    if kind == "tool_call":
        return {
            "updateType": "tool_call",
            "toolCallId": payload.get("tool_call_id", ""),
            "title": payload.get("title", payload.get("tool", "")),
        }
    if kind == "tool_result":
        if payload.get("status") == "diff":
            return {
                "updateType": "diff",
                "path": payload.get("path", ""),
                "oldText": payload.get("old_text", ""),
                "newText": payload.get("new_text", ""),
            }
        return {
            "updateType": "tool_call_update",
            "toolCallId": payload.get("tool_call_id", ""),
            "status": payload.get("status", ""),
        }
    if kind == "approval_request":
        return {
            "updateType": "approval_request",
            "approvalId": payload.get("approval_id", ""),
            "action": payload.get("action", ""),
        }
    if kind == "error":
        return {"updateType": "error", "message": payload.get("message", "")}
    return None


async def serve_stdio(make_runtime: MakeRuntime) -> int:
    """Run the framing loop over real stdin/stdout. Returns a process exit code."""
    loop = asyncio.get_running_loop()
    outbox: asyncio.Queue[bytes] = asyncio.Queue()

    async def _send(frame: bytes) -> None:
        outbox.put_nowait(frame)

    async def _writer() -> None:
        while True:
            await loop.run_in_executor(None, _write_stdout, await outbox.get())

    server = AcpServer(make_runtime, _send)
    writer = asyncio.ensure_future(_writer())
    buffer = b""
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
                await server.handle(message)
    except (AcpProtocolError, OSError):
        pass
    finally:
        writer.cancel()
        for session in list(server._sessions.values()):
            try:
                await session.close()
            except Exception:
                pass
    return 0


def _write_stdout(frame: bytes) -> None:
    sys.stdout.buffer.write(frame)
    sys.stdout.buffer.flush()


async def make_echo_runtime(session_id: str) -> Any:
    """Deterministic driver for conformance and smoke tests. Not for real work."""
    from garuda.runtime.fake import FakeRuntime, FakeScenario

    return FakeRuntime(FakeScenario.SUCCESS, runtime_id="garuda-echo")


async def make_native_runtime(session_id: str, *, workspace: str = ".") -> Any:
    """Production driver: the native loop behind the runtime boundary."""
    import os

    from garuda.agents.setup import prepare_agent_run
    from garuda.core.events import EventStore
    from garuda.interfaces.runner import resolve_environment
    from garuda.model.litellm_model import LitellmModel
    from garuda.model.protocol import DEFAULT_MODEL, MODEL_ENV_VAR
    from garuda.runtime.native import NativeGarudaRuntime

    profile, config, permissions, tools, agent, mcp_manager = await prepare_agent_run(
        "build", workspace=workspace
    )
    model = LitellmModel(model_name=os.environ.get(MODEL_ENV_VAR, DEFAULT_MODEL))
    runtime = NativeGarudaRuntime(
        agent=agent, model=model, tools=tools, config=config,
        permissions=permissions,
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


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Garuda as an ACP server (stdio only).")
    parser.add_argument("--driver", choices=("echo", "native"), default="echo")
    parser.add_argument("--workspace", default=".")
    parser.add_argument(
        "--listen",
        default=None,
        help="Refused: this server is stdio-only. No network listener exists.",
    )
    args = parser.parse_args(argv)
    if args.listen is not None:
        print(
            "error: network listeners are not supported; "
            "run behind stdio only (explicit secure configuration required).",
            file=sys.stderr,
        )
        return 2
    if args.driver == "native":
        import functools

        make_runtime = functools.partial(make_native_runtime, workspace=args.workspace)
    else:
        make_runtime = make_echo_runtime
    return asyncio.run(serve_stdio(make_runtime))


if __name__ == "__main__":
    raise SystemExit(main())
