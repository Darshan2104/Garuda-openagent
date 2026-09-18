"""Inbound ACP server tests for issue #47 (P2.1).

Conformance through real subprocesses, inbound/outbound isolation, stable
sessions with cancellation, and the no-listener refusal.
"""

import asyncio
import sys

import pytest

from garuda.acp.protocol import ACP_VERSION, decode_frame, encode_frame
from garuda.acp.server import AcpServer, main, make_echo_runtime


class Duplex:
    """In-process client/server pair speaking framed ACP without a socket."""

    def __init__(self, server: AcpServer):
        self._server = server
        self.sent: list[bytes] = []
        self._next_id = 0

    async def send(self, frame: bytes) -> None:
        self.sent.append(frame)

    async def call(self, method: str, params: dict | None = None) -> dict:
        self._next_id += 1
        call_id = self._next_id
        before = len(self.sent)
        await self._server.handle(
            {"jsonrpc": "2.0", "id": call_id, "method": method, "params": params or {}}
        )
        replies = []
        for frame in self.sent[before:]:
            message, _ = decode_frame(frame)
            if message.get("id") == call_id:
                replies.append(message)
        assert len(replies) == 1, replies
        if "error" in replies[0]:
            raise AssertionError(replies[0]["error"]["message"])
        return replies[0]["result"]

    def notifications(self) -> list[dict]:
        notes = []
        for frame in self.sent:
            message, _ = decode_frame(frame)
            if message.get("method") == "session/update":
                notes.append(message["params"]["update"])
        return notes


def _server() -> tuple[AcpServer, Duplex]:
    sent: list[bytes] = []

    async def _send(frame: bytes) -> None:
        sent.append(frame)

    server = AcpServer(make_echo_runtime, _send)
    duplex = Duplex(server)
    duplex.sent = sent
    return server, duplex


async def test_initialize_rejects_foreign_versions():
    _, duplex = _server()
    result = await duplex.call("initialize", {"protocolVersion": ACP_VERSION})
    assert result["protocolVersion"] == ACP_VERSION
    assert result["agentCapabilities"]["families"]
    with pytest.raises(AssertionError, match="99.99"):
        await duplex.call("initialize", {"protocolVersion": "99.99"})


async def test_sessions_prompts_and_cancellation():
    server, duplex = _server()
    created = await duplex.call("session/new", {})
    session_id = created["sessionId"]
    assert session_id.startswith("garuda-")
    assert server.session_count() == 1

    result = await duplex.call(
        "session/prompt", {"sessionId": session_id, "prompt": "hello inbound"}
    )
    assert result["stopReason"] == "end_turn"
    updates = duplex.notifications()
    assert any(u.get("text", "").startswith("done:") for u in updates), updates

    await duplex.call("session/cancel", {"sessionId": session_id})
    with pytest.raises(AssertionError, match="unknown session"):
        await duplex.call("session/prompt", {"sessionId": "garuda-nope", "prompt": "x"})


async def test_unknown_methods_fail_closed_not_hung():
    server, duplex = _server()
    before = len(duplex.sent)
    await server.handle({"jsonrpc": "2.0", "id": 99, "method": "teleport", "params": {}})
    message, _ = decode_frame(duplex.sent[before])
    assert message["id"] == 99
    assert "unsupported method" in message["error"]["message"]


async def test_inbound_outbound_isolation():
    from garuda.acp import client as outbound

    server, duplex = _server()
    assert not hasattr(server, "_process")
    assert outbound.AcpProcess.__module__ == "garuda.acp.client"
    first = await duplex.call("session/new", {})
    second = await duplex.call("session/new", {})
    assert first["sessionId"] != second["sessionId"]
    assert server.session_count() == 2


async def _read_reply(stdout, call_id: int) -> dict:
    """Read framed output until the reply with `call_id` arrives."""
    buffer = b""
    while True:
        while True:
            try:
                message, buffer = decode_frame(buffer)
            except ValueError:
                break
            if message.get("id") == call_id:
                return message
        chunk = await asyncio.wait_for(stdout.read(65536), 10)
        if not chunk:
            raise AssertionError("server closed stdout")
        buffer += chunk


async def test_subprocess_conformance_against_echo_driver():
    argv = [sys.executable, "-m", "garuda.acp.server", "--driver", "echo"]
    process = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    assert process.stdin is not None and process.stdout is not None
    try:
        process.stdin.write(
            encode_frame({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        )
        await process.stdin.drain()
        assert (await _read_reply(process.stdout, 1))["result"][
            "protocolVersion"
        ] == ACP_VERSION

        process.stdin.write(
            encode_frame({"jsonrpc": "2.0", "id": 2, "method": "session/new", "params": {}})
        )
        await process.stdin.drain()
        created = await _read_reply(process.stdout, 2)
        assert created["result"]["sessionId"].startswith("garuda-")

        process.stdin.write(
            encode_frame(
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "session/prompt",
                    "params": {
                        "sessionId": created["result"]["sessionId"],
                        "prompt": "hello",
                    },
                }
            )
        )
        await process.stdin.drain()
        assert (await _read_reply(process.stdout, 3))["result"]["stopReason"] == "end_turn"
    finally:
        try:
            process.terminate()
            await asyncio.wait_for(process.wait(), 10)
        except (TimeoutError, ProcessLookupError):
            pass


def test_no_listener_without_secure_config(capsys):
    assert main(["--listen", "0.0.0.0:9999"]) == 2
    assert "stdio only" in capsys.readouterr().err
    source = open("garuda/acp/server.py").read()
    for marker in ("import socket", "socket.socket", "socketserver", "socket.create"):
        assert marker not in source, marker
