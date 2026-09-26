"""Inbound ACP server tests for issue #47 (P2.1).

Conformance through real subprocesses, inbound/outbound isolation, stable
sessions with cancellation, and the no-listener refusal.
"""

import asyncio
import sys

import pytest

from garuda.acp.protocol import ACP_VERSION, decode_frame, encode_frame
from garuda.acp.server import (
    AcpServer,
    _cli_parser,
    _select_driver,
    main,
    make_echo_runtime,
    make_native_runtime,
)


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


async def test_session_methods_require_the_handshake():
    _, duplex = _server()
    with pytest.raises(AssertionError, match="initialize first"):
        await duplex.call("session/new", {})


async def test_native_is_the_product_default():
    assert _cli_parser().parse_args([]).driver == "native"
    assert _select_driver("echo", ".") is make_echo_runtime
    native = _select_driver("native", ".")
    assert native.func is make_native_runtime
    assert native.keywords == {"workspace": "."}


async def test_sessions_prompts_and_cancellation():
    server, duplex = _server()
    await duplex.call("initialize", {"protocolVersion": ACP_VERSION})
    created = await duplex.call("session/new", {})
    session_id = created["sessionId"]
    assert session_id.startswith("garuda-")
    assert server.session_count() == 1

    result = await duplex.call(
        "session/prompt", {
            "sessionId": session_id,
            "prompt": [{"type": "text", "text": "hello inbound"}],
        }
    )
    assert result["stopReason"] == "end_turn"
    updates = duplex.notifications()
    assert any(
        (u.get("content", {}).get("text", "")).startswith("done:") for u in updates
    ), updates

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
    await duplex.call("initialize", {"protocolVersion": ACP_VERSION})
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
            encode_frame({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                          "params": {"protocolVersion": ACP_VERSION}})
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
                    "prompt": [{"type": "text", "text": "hello"}],
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


async def test_cancel_processed_while_prompt_blocked():
    """A cancel arriving mid-turn interrupts the prompt instead of queueing
    behind it: the prompt task and the inline cancel overlap in time."""
    from garuda.acp.protocol import AcpCancelledError
    from garuda.acp.server import MAX_INFLIGHT_TASKS

    assert MAX_INFLIGHT_TASKS > 0

    class BlockingRuntime:
        runtime_id = "blocking"

        def __init__(self):
            self._release = asyncio.Event()
            self._cancelled = False
            self.prompt_calls = 0

        async def start(self, *, task, session_id=None):
            return None

        async def prompt(self, text):
            self.prompt_calls += 1
            await self._release.wait()
            if self._cancelled:
                raise AcpCancelledError("cancelled by client")
            return 1

        async def cancel(self, *, reason=""):
            self._cancelled = True
            self._release.set()

        async def permission_response(self, *, approval_id, allow):
            raise AssertionError("no approvals in this test")

        async def poll_events(self, cursor):
            return [], 0

        async def close(self):
            self._release.set()

    sent: list[bytes] = []

    async def _send(frame: bytes) -> None:
        sent.append(frame)

    async def _make(session_id: str):
        return BlockingRuntime()

    server = AcpServer(_make, _send)
    await server.handle(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": ACP_VERSION}}
    )
    await server.handle({"jsonrpc": "2.0", "id": 2, "method": "session/new", "params": {}})
    created, _ = decode_frame(sent[-1])
    session_id = created["result"]["sessionId"]

    prompt_task = asyncio.ensure_future(
        server.handle(
            {"jsonrpc": "2.0", "id": 3, "method": "session/prompt",
             "params": {"sessionId": session_id,
                        "prompt": [{"type": "text", "text": "blocked work"}]}}
        )
    )
    await asyncio.sleep(0.2)
    assert not prompt_task.done(), "prompt must still be blocked"
    await server.handle(
        {"jsonrpc": "2.0", "id": 4, "method": "session/cancel",
         "params": {"sessionId": session_id}}
    )
    await asyncio.wait_for(prompt_task, 10)
    replies = {}
    for frame in sent:
        message, _ = decode_frame(frame)
        if "id" in message and message["id"] in (3, 4):
            replies[message["id"]] = message
    assert replies[4]["result"] == {"cancelled": True}
    assert replies[3]["result"] == {"stopReason": "cancelled"}


async def test_permission_request_roundtrip_streams_mid_turn():
    """The server emits an ACP request while the runtime waits for approval."""
    from garuda.runtime.events import RuntimeEvent, RuntimeEventKind

    class ApprovalRuntime:
        runtime_id = "approval"

        def __init__(self):
            self._approved = asyncio.Event()
            self._events = []

        async def start(self, *, task, session_id=None):
            self._session_id = session_id

        async def prompt(self, text):
            self._events.append(RuntimeEvent(
                kind=RuntimeEventKind.APPROVAL_REQUEST,
                session_id=self._session_id, turn=1, seq=0,
                payload={"approval_id": "a1", "action": text},
            ))
            await self._approved.wait()
            self._events.append(RuntimeEvent(
                kind=RuntimeEventKind.MESSAGE,
                session_id=self._session_id, turn=1, seq=1,
                payload={"chunk": "approved"},
            ))
            return 1

        async def permission_response(self, *, approval_id, allow):
            assert approval_id == "a1" and allow is True
            self._approved.set()

        async def poll_events(self, cursor):
            return self._events[cursor:], len(self._events)

        async def cancel(self, *, reason=""):
            self._approved.set()

        async def close(self):
            self._approved.set()

    sent: list[bytes] = []

    async def _send(frame: bytes) -> None:
        sent.append(frame)

    async def _make(session_id: str):
        return ApprovalRuntime()

    server = AcpServer(_make, _send)
    await server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                         "params": {"protocolVersion": ACP_VERSION}})
    await server.handle({"jsonrpc": "2.0", "id": 2, "method": "session/new", "params": {}})
    created, _ = decode_frame(sent[-1])
    session_id = created["result"]["sessionId"]
    prompt_task = asyncio.create_task(server.handle({
        "jsonrpc": "2.0", "id": 3, "method": "session/prompt",
        "params": {"sessionId": session_id,
                    "prompt": [{"type": "text", "text": "write file"}]},
    }))
    request = None
    for _ in range(100):
        await asyncio.sleep(0.01)
        for frame in sent:
            message, _ = decode_frame(frame)
            if message.get("method") == "session/request_permission":
                request = message
                break
        if request:
            break
    assert request is not None
    assert request["params"]["approvalId"] == "a1"
    await server.handle({"jsonrpc": "2.0", "id": request["id"],
                         "result": {"approved": True}})
    await asyncio.wait_for(prompt_task, 5)
    replies = [decode_frame(frame)[0] for frame in sent if decode_frame(frame)[0].get("id") == 3]
    assert replies[-1]["result"]["stopReason"] == "end_turn"


async def test_malformed_framing_exits_nonzero_without_details():
    argv = [sys.executable, "-m", "garuda.acp.server", "--driver", "echo"]
    process = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    assert process.stdin is not None
    try:
        process.stdin.write(b"not-framed\r\n\r\n{}")
        await process.stdin.drain()
        process.stdin.close()
        code = await asyncio.wait_for(process.wait(), 15)
        assert code == 2, code
        _, stderr = await asyncio.gather(
            process.stdout.read(), process.stderr.read()
        )
        assert b"Traceback" not in stderr
        assert b"malformed input frame" in stderr
    finally:
        try:
            process.terminate()
        except ProcessLookupError:
            pass


async def test_internal_errors_do_not_leak():
    _, duplex = _server()
    await duplex.call("initialize", {"protocolVersion": ACP_VERSION})

    class ExplodingRuntime:
        runtime_id = "exploding"

        async def start(self, *, task, session_id=None):
            raise RuntimeError("secret stack with passwords")

    async def _make(session_id: str):
        return ExplodingRuntime()

    from garuda.acp.server import AcpServer as Server

    sent: list[bytes] = []

    async def _send(frame: bytes) -> None:
        sent.append(frame)

    server = Server(_make, _send)
    await server.handle(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": ACP_VERSION}}
    )
    await server.handle({"jsonrpc": "2.0", "id": 2, "method": "session/new", "params": {}})
    message, _ = decode_frame(sent[-1])
    assert "error" in message
    assert "secret stack" not in message["error"]["message"]
    assert message["error"]["message"] == "internal error"


def test_no_listener_without_secure_config(capsys):
    assert main(["--listen", "0.0.0.0:9999"]) == 2
    assert "stdio only" in capsys.readouterr().err
    source = open("garuda/acp/server.py").read()
    for marker in ("import socket", "socket.socket", "socketserver", "socket.create"):
        assert marker not in source, marker
