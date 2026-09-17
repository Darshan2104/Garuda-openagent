"""ACP subprocess tests for issue #21 (P0.12).

Framing, timeout, malformed stream, cancellation, and cleanup fixtures run
against real fake-agent subprocesses; the codec units need no process at all.
"""

import asyncio
import json
import os
import sys

import pytest

from garuda.acp.client import AcpProcess
from garuda.acp.protocol import (
    AcpCancelledError,
    AcpExitError,
    AcpProtocolError,
    AcpTimeoutError,
    decode_frame,
    encode_frame,
)

ECHO_SERVER = r"""
import json, sys

def read_frame():
    header = b""
    while b"\r\n\r\n" not in header:
        chunk = sys.stdin.buffer.read(1)
        if not chunk:
            raise EOFError
        header += chunk
    head, _, rest = header.partition(b"\r\n\r\n")
    length = int(head.split(b":")[1])
    body = rest
    while len(body) < length:
        body += sys.stdin.buffer.read(length - len(body))
    return json.loads(body)

def send(message):
    body = json.dumps(message).encode()
    sys.stdout.buffer.write(b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body)
    sys.stdout.buffer.flush()

RESULTS = json.loads(sys.argv[1])
try:
    while True:
        request = read_frame()
        if "id" in request:
            method = request.get("method", "")
            send({"jsonrpc": "2.0", "id": request["id"], "result": RESULTS.get(method, {})})
        send({"jsonrpc": "2.0", "method": "session/update", "params": {"seen": request.get("method")}})
except EOFError:
    pass
"""

GUARD_VAR = "GARUDA_ACP_TEST_PARENT_SECRET"


def _argv(payload: dict, extra: str = "") -> list[str]:
    return [sys.executable, "-c", extra + ECHO_SERVER, json.dumps(payload)]


async def _launched(argv: list[str], **kwargs) -> AcpProcess:
    process = AcpProcess(argv, **kwargs)
    await process.launch()
    return process


def test_frame_codec_round_trip_and_rejects():
    message = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
    decoded, rest = decode_frame(encode_frame(message) + b"leftover")
    assert decoded == message
    assert rest == b"leftover"
    with pytest.raises(ValueError):
        decode_frame(b"Content-Length: 10\r\n\r\nabc")
    with pytest.raises(AcpProtocolError):
        decode_frame(b"no headers here\r\n\r\n{}")
    with pytest.raises(AcpProtocolError):
        decode_frame(encode_frame([1, 2, 3]))
    with pytest.raises(AcpProtocolError):
        decode_frame(b"Content-Length: xyz\r\n\r\n{}")


async def test_handshake_session_prompt_and_notifications():
    process = await _launched(
        _argv(
            {
                "initialize": {"protocolVersion": "0.4", "ok": True},
                "session/new": {"sessionId": "s1"},
            }
        )
    )
    try:
        result = await process.initialize()
        assert result["protocolVersion"] == "0.4"
        session_id = await process.session_new()
        assert session_id == "s1"
        await process.session_prompt(session_id, "hello")
        seen: set[str] = set()
        for _ in range(100):
            seen |= {n["params"]["seen"] for n in process.drain_notifications()}
            if {"initialize", "session/new", "session/prompt"} <= seen:
                break
            await asyncio.sleep(0.05)
        assert {"initialize", "session/new", "session/prompt"} <= seen
    finally:
        await process.close()
    assert not process.is_running


async def test_stderr_never_corrupts_the_stream():
    script = "import sys; sys.stderr.write('diagnostic line\\n'); sys.stderr.flush()\n" + ECHO_SERVER
    process = await _launched(
        _argv({"initialize": {"protocolVersion": "0.4", "ok": True}}, extra=script)
    )
    try:
        assert (await process.initialize())["ok"] is True
        assert "diagnostic line" in process.stderr_tail
    finally:
        await process.close()


async def test_child_gets_minimal_env_not_parent_secrets():
    os.environ[GUARD_VAR] = "must-not-cross"
    script = (
        "import json, os, sys; "
        "sys.stdout.buffer.write(json.dumps(dict(os.environ)).encode()); "
        "sys.stdout.buffer.flush()\n"
    )
    argv = [sys.executable, "-c", script]
    process = await _launched(argv, extra_env={"GARUDA_CHILD": "yes"})
    try:
        raw = await asyncio.wait_for(process._process.stdout.read(65536), 10)
        child_env = json.loads(raw)
        assert GUARD_VAR not in child_env
        assert child_env.get("GARUDA_CHILD") == "yes"
    finally:
        del os.environ[GUARD_VAR]
        await process.close()


async def test_malformed_stream_is_typed():
    argv = [
        sys.executable,
        "-c",
        "import sys, time; "
        "sys.stdout.buffer.write(b'Content-Length: xyz\\r\\n\\r\\n{}'); "
        "sys.stdout.buffer.flush(); time.sleep(30)",
    ]
    process = await _launched(argv)
    try:
        with pytest.raises(AcpProtocolError):
            await asyncio.wait_for(process.initialize(), 10)
    finally:
        await process.close()


async def test_slow_agent_hits_the_deadline():
    argv = [sys.executable, "-c", "import time; time.sleep(30)"]
    process = await _launched(argv, call_timeout=0.3)
    try:
        with pytest.raises(AcpTimeoutError):
            await process.initialize()
    finally:
        await process.close()
    assert not process.is_running


async def test_exit_becomes_typed_error_with_stderr():
    argv = [
        sys.executable,
        "-c",
        "import sys; sys.stderr.write('boom detail\\n'); sys.exit(3)",
    ]
    process = await _launched(argv)
    try:
        with pytest.raises(AcpExitError) as exc:
            await asyncio.wait_for(process.initialize(), 10)
        assert exc.value.exit_code == 3
        assert "boom detail" in exc.value.stderr_tail
    finally:
        await process.close()


async def test_cancel_fails_pending_with_cancelled():
    script = "import time; time.sleep(30)"
    process = await _launched([sys.executable, "-c", script], call_timeout=20)
    try:
        pending = asyncio.ensure_future(process._call("session/prompt", {}))
        await asyncio.sleep(0.3)
        await process.session_cancel("s1")
        with pytest.raises(AcpCancelledError):
            await pending
    finally:
        await process.close()


async def test_close_reaps_the_group_and_is_idempotent():
    process = await _launched(
        _argv({"initialize": {"ok": True}}), call_timeout=5
    )
    pid = process.pid
    assert pid
    await process.close()
    await process.close()
    assert not process.is_running
    with pytest.raises(ProcessLookupError):
        os.killpg(pid, 0)
