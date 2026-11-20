"""Inbound ACP server tests for issue #47 (P2.1).

Conformance through real subprocesses, inbound/outbound isolation, stable
sessions with cancellation, and the no-listener refusal.
"""

import asyncio
import io
import os
import subprocess
import sys
import time

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


async def test_prompt_delivers_all_text_blocks_and_refuses_invalid_content():
    _, duplex = _server()
    await duplex.call("initialize", {"protocolVersion": ACP_VERSION})
    session = await duplex.call("session/new")
    first = {"type": "text", "text": "Summarize the files."}
    second = {"type": "text", "text": "Include the test results."}
    blocked = {"type": "text", "text": "Do not dispatch a truncated request."}
    for invalid, diagnostic in [
        ({"type": "image", "mimeType": "image/png", "data": "AA=="}, "unsupported"),
        ({"type": "text", "text": 7}, "text"),
    ]:
        with pytest.raises(AssertionError, match=diagnostic):
            await duplex.call("session/prompt", {**session, "prompt": [blocked, invalid]})
    assert not duplex.notifications()

    result = await duplex.call("session/prompt", {**session, "prompt": [first, second]})
    assert result["stopReason"] == "end_turn"
    texts = [note.get("content", {}).get("text", "") for note in duplex.notifications()]
    assert "done: Summarize the files.\n\nInclude the test results." in texts


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
    from garuda.acp import AcpServer as PackageServer
    from garuda.acp import client as outbound

    server, duplex = _server()
    assert PackageServer is type(server)
    assert not hasattr(server, "_process")
    assert outbound.AcpProcess.__module__ == "garuda.acp.client"
    await duplex.call("initialize", {"protocolVersion": ACP_VERSION})
    first = await duplex.call("session/new", {})
    second = await duplex.call("session/new", {})
    assert first["sessionId"] != second["sessionId"]
    assert server.session_count() == 2


async def test_parallel_prompts_send_updates_for_the_owning_session():
    _, duplex = _server()
    await duplex.call("initialize", {"protocolVersion": ACP_VERSION})
    first = await duplex.call("session/new")
    second = await duplex.call("session/new")

    async def prompt(session, text):
        return await duplex.call("session/prompt", {
            **session, "prompt": [{"type": "text", "text": text}],
        })

    replies = await asyncio.gather(prompt(first, "first task"), prompt(second, "second task"))
    assert replies == [{"stopReason": "end_turn"}, {"stopReason": "end_turn"}]
    routed = {}
    for frame in duplex.sent:
        message, _ = decode_frame(frame)
        if message.get("method") == "session/update":
            params = message["params"]
            content = params["update"].get("content", {})
            if content.get("type") == "text":
                routed[params["sessionId"]] = content["text"]
    assert routed == {
        first["sessionId"]: "done: first task",
        second["sessionId"]: "done: second task",
    }


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


@pytest.mark.parametrize("framing", ["large", "blank-lines"])
async def test_stdio_accepts_fragmented_large_and_blank_lines(framing):
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "garuda.acp.server", "--driver", "echo",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        process.stdin.write(encode_frame({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": ACP_VERSION},
        }))
        await process.stdin.drain()
        assert "result" in await _read_reply(process.stdout, 1)
        frame = encode_frame({
            "jsonrpc": "2.0", "id": 2, "method": "session/new",
            "params": {"pad": "x" * 100_000} if framing == "large" else {},
        })
        process.stdin.write((b"\n\r\n \t\r\n" if framing == "blank-lines" else b"") + frame)
        await process.stdin.drain()
        reply = await _read_reply(process.stdout, 2)
        assert reply["result"]["sessionId"].startswith("garuda-")
        process.stdin.close()
        assert await asyncio.wait_for(process.wait(), 10) == 0
    finally:
        if process.returncode is None:
            process.kill()
        await process.wait()


def test_module_launch_has_no_runpy_warning():
    result = subprocess.run(
        [sys.executable, "-m", "garuda.acp.server", "--help"],
        capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0
    assert result.stderr == ""


async def test_stdio_drains_replies_to_large_batch_before_eof_exit():
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "garuda.acp.server", "--driver", "echo",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    call_ids = list(range(1, 66))
    batch = encode_frame({
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": ACP_VERSION, "clientInfo": {"pad": "x" * 100_000}},
    }) + b"".join(
        encode_frame({"jsonrpc": "2.0", "id": call_id, "method": "session/new", "params": {}})
        for call_id in call_ids[1:]
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(batch), 10)
        assert process.returncode == 0, stderr
        replies = [decode_frame(line + b"\n")[0] for line in stdout.splitlines()]
        assert [reply["id"] for reply in replies] == call_ids
        assert all("sessionId" in reply["result"] for reply in replies[1:])
    finally:
        if process.returncode is None:
            process.kill()
        await process.wait()


async def test_stdio_eof_allows_session_start_beyond_output_deadline():
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-c", """
import asyncio
from garuda.acp import server

server.CLEANUP_TIMEOUT = 0.1

async def make_runtime(session_id):
    await asyncio.sleep(0.3)
    return await server.make_echo_runtime(session_id)

raise SystemExit(asyncio.run(server.serve_stdio(make_runtime)))
""",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    batch = encode_frame({
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": ACP_VERSION},
    }) + encode_frame({"jsonrpc": "2.0", "id": 2, "method": "session/new", "params": {}})
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(batch), 10)
        assert process.returncode == 0, stderr
        replies = [decode_frame(line + b"\n")[0] for line in stdout.splitlines()]
        assert [reply["id"] for reply in replies] == [1, 2]
        assert replies[0]["result"]["protocolVersion"] == ACP_VERSION
        assert replies[1]["result"]["sessionId"].startswith("garuda-")
    finally:
        if process.returncode is None:
            process.kill()
        await process.wait()


async def test_stdio_slow_reader_keeps_all_replies_while_input_is_open():
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-c",
        "from garuda.acp import server; server.CLEANUP_TIMEOUT = 0.1; "
        "raise SystemExit(server.main(['--driver', 'echo']))",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE, limit=1024,
    )
    try:
        # Warm up the driver before the pause so startup time cannot hide a stall.
        for call_id, method, params in (
            (1, "initialize", {"protocolVersion": ACP_VERSION}),
            (2, "session/new", {}),
        ):
            process.stdin.write(encode_frame({
                "jsonrpc": "2.0", "id": call_id, "method": method, "params": params,
            }))
            await process.stdin.drain()
            assert "result" in await _read_reply(process.stdout, call_id)

        call_ids = list(range(3, 3003))
        process.stdin.write(b"".join(encode_frame({
            "jsonrpc": "2.0", "id": call_id, "method": "session/new", "params": {},
        }) for call_id in call_ids))
        # The small StreamReader limit ensures its transport also stops reading,
        # so the real stdout pipe and the server's output queue fill up.
        await asyncio.sleep(1)
        replies = []
        for _ in call_ids:
            line = await asyncio.wait_for(process.stdout.readline(), 10)
            if not line:
                break
            message, rest = decode_frame(line)
            assert line.endswith(b"\n") and rest == b""
            replies.append(message)
        assert [reply["id"] for reply in replies] == call_ids
        assert all("sessionId" in reply["result"] for reply in replies)
        await process.stdin.drain()
        process.stdin.close()
        assert await asyncio.wait_for(process.wait(), 10) == 0
        assert await process.stdout.read() == b""
        assert await process.stderr.read() == b""
    finally:
        if process.returncode is None:
            process.kill()
        await process.wait()
        process.stdin.close()


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
    updates = []
    for frame in sent:
        message, _ = decode_frame(frame)
        if "id" in message and message["id"] in (3, 4):
            replies[message["id"]] = message
        if message.get("method") == "session/update":
            updates.append(message["params"])
    assert replies[4]["result"] == {"cancelled": True}
    assert replies[3]["result"] == {"stopReason": "cancelled"}
    assert any(update.get("sessionId") == session_id and update["update"]["sessionUpdate"] == "error"
               for update in updates)


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
    await server.handle({"jsonrpc": "2.0", "id": request["id"], "result": {"approved": True}})
    await server.handle({"jsonrpc": "2.0", "id": "unknown", "error": {"code": -1}})
    await server.handle({"jsonrpc": "2.0", "id": ["invalid"], "result": {}})
    await server.handle({"jsonrpc": "2.0", "id": 4, "method": "session/new", "params": {}})
    assert decode_frame(sent[-1])[0]["result"]["sessionId"] != session_id


@pytest.mark.parametrize("frame", [
    b"not-framed\r\n\r\n{}",
    encode_frame({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                  "params": {"protocolVersion": ACP_VERSION}})[:-1],
])
async def test_malformed_framing_exits_nonzero_without_details(frame):
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
        process.stdin.write(frame)
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


async def test_stdio_eof_does_not_wait_for_dead_writer(monkeypatch):
    from garuda.acp.server import serve_stdio

    frame = encode_frame({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                          "params": {"protocolVersion": ACP_VERSION}})

    class BrokenOutput:
        def write(self, data):
            raise BrokenPipeError("client closed stdout")

        def flush(self):
            pass

    monkeypatch.setattr(sys, "stdin", type("Input", (), {"buffer": io.BytesIO(frame * 100)})())
    monkeypatch.setattr(sys, "stdout", type("Output", (), {"buffer": BrokenOutput()})())
    started = time.monotonic()
    code = await asyncio.wait_for(serve_stdio(make_echo_runtime), 10)
    assert code == 1
    assert time.monotonic() - started < 1, "a dead writer cannot drain its remaining queue"


async def test_stdio_eof_drain_deadline_cancels_pending_pipe_io(monkeypatch):
    from garuda.acp.server import serve_stdio

    frame = encode_frame({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                          "params": {"protocolVersion": ACP_VERSION}})
    read_fd, write_fd = os.pipe()
    try:
        os.set_blocking(write_fd, False)
        with pytest.raises(BlockingIOError):
            while True:
                os.write(write_fd, b" " * 1024)
        os.set_blocking(write_fd, True)

        with os.fdopen(write_fd, "wb", closefd=False) as pipe:
            monkeypatch.setattr(sys, "stdin", type("Input", (), {"buffer": io.BytesIO(frame)})())
            monkeypatch.setattr(sys, "stdout", type("Output", (), {"buffer": pipe})())
            monkeypatch.setattr("garuda.acp.server.CLEANUP_TIMEOUT", 0.1)
            assert await asyncio.wait_for(serve_stdio(make_echo_runtime), 2) == 1
            assert os.get_blocking(write_fd)
    finally:
        os.close(write_fd)
        os.close(read_fd)


async def test_stdio_stalled_output_settles_without_sending_an_error_reply(monkeypatch):
    from garuda.acp.server import serve_stdio

    # Span multiple input reads: EOF must be observed while dispatch is blocked
    # on the full output queue, rather than only after dispatch completes.
    frames = b"".join(encode_frame({
        "jsonrpc": "2.0", "id": call_id, "method": "initialize",
        "params": {"protocolVersion": ACP_VERSION},
    }) for call_id in range(1, 1001))
    writer_settled = asyncio.Event()
    error_replies = []

    async def stalled_write(output, fd, frame):
        try:
            await asyncio.Future()
        finally:
            writer_settled.set()

    async def record_error(self, call_id, message):
        error_replies.append((call_id, message))

    monkeypatch.setattr(sys, "stdin", type("Input", (), {"buffer": io.BytesIO(frames)})())
    monkeypatch.setattr(sys, "stdout", type("Output", (), {"buffer": io.BytesIO()})())
    monkeypatch.setattr("garuda.acp.server._write_stdout", stalled_write)
    monkeypatch.setattr("garuda.acp.server.CLEANUP_TIMEOUT", 0.1)
    monkeypatch.setattr("garuda.acp.server.MAX_QUEUED_REPLIES", 1)
    monkeypatch.setattr(AcpServer, "_fail", record_error)
    assert await asyncio.wait_for(serve_stdio(make_echo_runtime), 2) == 1
    assert writer_settled.is_set()
    assert error_replies == []


async def test_stdio_closes_prompt_session_when_output_fails_with_open_input(monkeypatch):
    from garuda.acp.server import serve_stdio
    from garuda.runtime.fake import FakeRuntime

    session_id = asyncio.get_running_loop().create_future()
    session_closed = asyncio.Event()

    class Runtime(FakeRuntime):
        async def close(self):
            await super().close()
            session_closed.set()

    class ReplySink:
        def write(self, data):
            message, _ = decode_frame(data)
            if message.get("id") == 2:
                session_id.set_result(message["result"]["sessionId"])
            elif message.get("id") != 1:
                raise BrokenPipeError("Output closed during the fixture prompt")

        def flush(self):
            pass

    async def make_runtime(_):
        return Runtime()

    def request(call_id, method, params):
        return encode_frame({"jsonrpc": "2.0", "id": call_id, "method": method, "params": params})

    read_fd, write_fd = os.pipe()
    with os.fdopen(read_fd, "rb") as source:
        monkeypatch.setattr(sys, "stdin", type("Input", (), {"buffer": source})())
        monkeypatch.setattr(sys, "stdout", type("Output", (), {"buffer": ReplySink()})())
        task = asyncio.create_task(serve_stdio(make_runtime))
        try:
            os.write(write_fd, request(1, "initialize", {"protocolVersion": ACP_VERSION}))
            os.write(write_fd, request(2, "session/new", {}))
            active_session = await asyncio.wait_for(session_id, 3)
            os.write(write_fd, request(3, "session/prompt", {
                "sessionId": active_session, "prompt": [{"type": "text", "text": "hello"}],
            }))
            assert await asyncio.wait_for(task, 3) == 1
            assert session_closed.is_set()
            assert os.get_blocking(source.fileno())
        finally:
            os.close(write_fd)
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_stdio_refuses_oversized_unterminated_frame_before_eof():
    from garuda.acp.protocol import MAX_FRAME_BYTES

    process = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "garuda.acp.server", "--driver", "echo",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        process.stdin.write(b"x" * (MAX_FRAME_BYTES + 1))
        try:
            await process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass
        assert await asyncio.wait_for(process.wait(), 10) == 2
        assert await process.stdout.read() == b""
        assert await process.stderr.read() == b"error: malformed input frame\n"
    finally:
        if process.returncode is None:
            process.kill()
        await process.wait()
        process.stdin.close()


async def test_stdio_waits_for_consumed_fragment_before_reading_remainder(monkeypatch):
    from garuda.acp import server as acp_server

    read_fd, write_fd = os.pipe()
    consumed = asyncio.Event()
    output = io.BytesIO()
    read_stdin = acp_server._read_stdin

    async def observe_read(source, fd):
        chunk = await read_stdin(source, fd)
        if chunk:
            consumed.set()
        return chunk

    pipe = os.fdopen(read_fd, "rb")
    monkeypatch.setattr(acp_server, "_read_stdin", observe_read)
    monkeypatch.setattr(sys, "stdin", type("Input", (), {"buffer": pipe})())
    monkeypatch.setattr(sys, "stdout", type("Output", (), {"buffer": output})())
    frame = encode_frame({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                          "params": {"protocolVersion": ACP_VERSION}})
    task = asyncio.create_task(acp_server.serve_stdio(make_echo_runtime))
    try:
        os.write(write_fd, frame[:20])
        # This acknowledgement comes from the real pipe read, so two writes
        # cannot coalesce into one server read even on a heavily loaded host.
        await asyncio.wait_for(consumed.wait(), 2)
        assert output.getvalue() == b""
        os.write(write_fd, frame[20:])
        os.close(write_fd)
        write_fd = None
        assert await asyncio.wait_for(task, 2) == 0
        reply, rest = decode_frame(output.getvalue())
        assert reply["id"] == 1 and reply["result"]["protocolVersion"] == ACP_VERSION
        assert rest == b""
    finally:
        if write_fd is not None:
            os.close(write_fd)
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        pipe.close()
