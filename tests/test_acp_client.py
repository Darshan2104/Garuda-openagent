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
    line = sys.stdin.buffer.readline()
    if not line:
        raise EOFError
    return json.loads(line)

def send(message):
    sys.stdout.buffer.write(json.dumps(message).encode() + b"\n")
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
    from garuda.acp.protocol import MAX_FRAME_BYTES

    message = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
    decoded, rest = decode_frame(encode_frame(message) + b"leftover")
    assert decoded == message
    assert rest == b"leftover"
    with pytest.raises(ValueError):
        decode_frame(b'{"jsonrpc":"2.0"}')
    with pytest.raises(AcpProtocolError):
        decode_frame(b"not-json\n")
    with pytest.raises(AcpProtocolError):
        decode_frame(b"[1, 2, 3]\n")
    with pytest.raises(AcpProtocolError):
        decode_frame(b'{"jsonrpc":"1.0"}\n')
    # A large message split across many reads is still one frame...
    big = encode_frame({"jsonrpc": "2.0", "method": "x", "params": {"t": "a" * 200_000}})
    with pytest.raises(ValueError):
        decode_frame(big[:150_000])
    assert decode_frame(big)[0]["params"]["t"] == "a" * 200_000
    # ...but fail-closed framing: an unterminated line cannot grow forever.
    with pytest.raises(AcpProtocolError, match="exceeds bound"):
        decode_frame(b"a" * (MAX_FRAME_BYTES + 1))


async def test_handshake_session_prompt_and_notifications():
    process = await _launched(
        _argv(
            {
                "initialize": {"protocolVersion": 1, "ok": True},
                "session/new": {"sessionId": "s1"},
            }
        )
    )
    try:
        result = await process.initialize()
        assert result["protocolVersion"] == 1
        session_id = await process.session_new()
        assert session_id == "s1"
        await process.session_prompt(session_id, "hello")
        notifications = [await process.next_notification(timeout=1.0) for _ in range(3)]
        assert all(notification is not None for notification in notifications)
        seen = {
            notification["params"]["seen"]
            for notification in notifications
            if notification is not None
        }
        assert {"initialize", "session/new", "session/prompt"} <= seen
    finally:
        await process.close()
    assert not process.is_running


@pytest.mark.parametrize("error", [None, {}, "not an error object", {"code": True, "message": "m"},
                                   {"code": -32603}, {"code": -32603, "message": []}])
async def test_malformed_error_response_settles_its_request_as_a_protocol_failure(error):
    script = (
        "import json,sys; request=json.loads(sys.stdin.readline()); "
        "print(json.dumps({'jsonrpc':'2.0','id':request['id'],'error':json.loads(sys.argv[1])}),flush=True); "
        "sys.stdin.read()"
    )
    process = await _launched([sys.executable, "-c", script, json.dumps(error)])
    try:
        with pytest.raises(AcpProtocolError) as failed:
            await asyncio.wait_for(process.initialize(), 1)
        assert type(failed.value) is AcpProtocolError
    finally:
        await process.close()


async def test_handshake_rejects_missing_or_mismatched_version():
    for result in ({}, {"protocolVersion": 99}):
        process = await _launched(_argv({"initialize": result}))
        try:
            with pytest.raises(AcpProtocolError, match="version mismatch"):
                await process.initialize()
        finally:
            await process.close()


async def test_stderr_never_corrupts_the_stream():
    script = "import sys; sys.stderr.write('diagnostic line\\n'); sys.stderr.flush()\n" + ECHO_SERVER
    process = await _launched(
        _argv({"initialize": {"ok": True, "protocolVersion": 1}}, extra=script)
    )
    try:
        assert (await process.initialize()) == {"ok": True, "protocolVersion": 1}
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
        # Claude Code on macOS finds its Keychain login by account name.
        assert child_env.get("USER") == os.environ.get("USER")
    finally:
        del os.environ[GUARD_VAR]
        await process.close()


async def test_malformed_stream_is_typed():
    argv = [
        sys.executable,
        "-c",
        "import sys, time; "
        "sys.stdout.buffer.write(b'not-json\\n'); "
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
    error = await _handshake_exit(argv)
    assert error.exit_code == 3
    assert "boom detail" in error.stderr_tail


async def test_preexited_agent_keeps_stderr_in_typed_error():
    argv = [
        sys.executable,
        "-c",
        "import sys; sys.stderr.write('pre-exit detail\\n'); sys.exit(4)",
    ]
    error = await _handshake_exit(argv, wait_for_exit=True)
    assert error.exit_code == 4
    assert "pre-exit detail" in error.stderr_tail


async def _handshake_exit(argv: list[str], *, wait_for_exit: bool = False) -> AcpExitError:
    """A fast exit is typed whether launch (leader gone before binding) or the
    handshake notices it first; which one wins is an OS scheduling race."""
    process = AcpProcess(argv)
    try:
        with pytest.raises(AcpExitError) as exc:
            await process.launch()
            if wait_for_exit:
                assert process._process is not None
                await process._process.wait()
            await asyncio.wait_for(process.initialize(), 10)
        return exc.value
    finally:
        await process.close()


def _spawn_reaped(monkeypatch) -> None:
    """Return the child only after it was reaped, and make the group lookup
    report ESRCH as macOS does for an exited leader (#644)."""
    import garuda.acp.client as client_module

    real_exec = asyncio.create_subprocess_exec

    async def exec_and_reap(*args, **kwargs):
        child = await real_exec(*args, **kwargs)
        await child.wait()
        return child

    def getpgid_gone(pid):
        raise ProcessLookupError(3, "No such process")

    monkeypatch.setattr(client_module.asyncio, "create_subprocess_exec", exec_and_reap)
    monkeypatch.setattr(client_module.os, "getpgid", getpgid_gone)


async def test_leader_exited_before_bind_reports_exit_code_and_stderr(monkeypatch):
    _spawn_reaped(monkeypatch)
    argv = [
        sys.executable,
        "-c",
        "import sys; sys.stderr.write('startup failed\\n'); sys.exit(3)",
    ]
    process = AcpProcess(argv)
    try:
        with pytest.raises(AcpExitError) as exc:
            await process.launch()
        assert exc.value.exit_code == 3
        assert "startup failed" in exc.value.stderr_tail
        assert "cannot bind" not in str(exc.value)
        assert not process.is_running
    finally:
        await process.close()


async def test_live_child_with_failed_group_lookup_is_still_refused(monkeypatch):
    import garuda.acp.client as client_module

    real_getpgid = os.getpgid
    calls = []

    def getpgid_gone_once(pid):
        # Only the bind lookup fails; the refusal path then finds the live group.
        calls.append(pid)
        if len(calls) == 1:
            raise ProcessLookupError(3, "No such process")
        return real_getpgid(pid)

    monkeypatch.setattr(client_module, "EARLY_EXIT_REAP_TIMEOUT", 0.2)
    monkeypatch.setattr(client_module.os, "getpgid", getpgid_gone_once)
    process = AcpProcess([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        with pytest.raises(AcpProtocolError, match="cannot bind ACP process group"):
            await process.launch()
        assert process._process is not None
        assert process._process.returncode is not None
        assert process._process.returncode < 0  # reaped by our kill, not an exit
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
        _argv({"initialize": {"ok": True, "protocolVersion": 1}}), call_timeout=5
    )
    pid = process.pid
    assert pid
    await process.close()
    await process.close()
    assert not process.is_running
    with pytest.raises(ProcessLookupError):
        os.killpg(pid, 0)


async def test_launch_after_close_is_refused():
    process = await _launched(
        _argv({"initialize": {"ok": True, "protocolVersion": 1}})
    )
    await process.close()
    with pytest.raises(AcpProtocolError, match="closed"):
        await process.launch()


async def test_cancel_notification_is_flushed_before_pending_fails():
    """`session/cancel` must reach the agent: `_notify` drains the transport."""
    received: list[bytes] = []

    class _DrainAssertingStdin:
        def __init__(self):
            self.drained = False

        def write(self, data: bytes):
            received.append(data)

        async def drain(self):
            self.drained = True

    process = AcpProcess([sys.executable, "-c", "pass"])
    fake_stdin = _DrainAssertingStdin()
    process._process = type(
        "_P", (), {"stdin": fake_stdin, "returncode": None}
    )()
    await process._notify("session/cancel", {"sessionId": "s9"})
    assert fake_stdin.drained
    assert received and b"session/cancel" in received[0]


async def test_agent_request_is_exposed_and_can_be_answered():
    received: list[bytes] = []

    class _Stdin:
        def write(self, data: bytes):
            received.append(data)

        async def drain(self):
            return None

    process = AcpProcess([sys.executable, "-c", "pass"])
    process._process = type("_P", (), {"stdin": _Stdin(), "returncode": None})()
    process._route(
        {
            "jsonrpc": "2.0",
            "id": 7,
            "method": "session/request_permission",
            "params": {"sessionId": "s1"},
        }
    )
    request = await process.next_request()
    assert request["id"] == 7
    await process.respond(7, result={"outcome": {"outcome": "cancelled"}})
    response, rest = decode_frame(received[0])
    assert not rest
    assert response["id"] == 7
    assert response["result"]["outcome"]["outcome"] == "cancelled"


async def test_prompt_turns_have_no_call_deadline():
    """The per-call deadline bounds handshake-style calls, not a prompt turn,
    which can legitimately outlast it (long edits, slow human approvals)."""
    process = AcpProcess(
        [sys.executable, "-m", "garuda.acp.fake_agent", "--profile", "streaming"],
        call_timeout=0.001,
    )
    try:
        await process.launch()
        await process.initialize()
        session_id = await process.session_new(cwd=os.getcwd())
        result = await process.session_prompt(session_id, "hi")
        assert result == {"stopReason": "end_turn"}
    finally:
        await process.close()


# --- remote error causes (#684) --------------------------------------------------------


def test_a_remote_error_keeps_its_data_cause_bounded():
    from garuda.acp.protocol import MAX_ERROR_DETAIL_CHARS, AcpRemoteError, remote_error

    plain = remote_error(-32603, "synthetic internal failure")
    assert type(plain) is AcpRemoteError
    assert str(plain) == "agent error -32603: synthetic internal failure"
    detailed = remote_error(-32603, "Internal error", {"message": "model\n  overloaded"})
    assert str(detailed) == "agent error -32603: Internal error: model overloaded"
    assert detailed.remote_code == -32603 and detailed.detail == "model overloaded"
    assert str(remote_error(-32603, "Internal error", {"details": "socket closed"})).endswith(
        "Internal error: socket closed")
    assert str(remote_error(-32603, "Internal error", {"other": 1})) == (
        "agent error -32603: Internal error")
    huge = remote_error(-32000, "x", "y" * 10_000)
    assert len(huge.detail) == MAX_ERROR_DETAIL_CHARS and huge.detail.endswith("…")


@pytest.mark.parametrize("data, reset", [
    ({"message": "You've hit your usage limit. Upgrade to Pro or try again at 3:05 PM.",
      "codexErrorInfo": "usageLimitExceeded"}, "3:05 PM"),
    ({"message": "Codex stopped this turn.", "codexErrorInfo": "usageLimitExceeded"}, None),
    ("Usage limit reached; try again in 2 hours", "2 hours"),
    ({"codexErrorInfo": "usageLimitExceeded"}, None),  # no words: still not "Internal error"
])
def test_a_usage_limit_cause_is_typed_and_quota_recovery_reads_it(data, reset):
    from garuda.acp.protocol import AcpUsageLimitError, remote_error
    from garuda.core.failure_evidence import classify_exception
    from garuda.flows.quota_recovery import NON_QUOTA, QUOTA_ERROR

    error = remote_error(-32603, "Internal error", data)
    assert isinstance(error, AcpUsageLimitError) and error.reset_hint == reset
    assert str(error).startswith("harness.limit_reached: ")
    assert classify_exception(error) == ("harness.limit_reached", "agent-response")
    reason = f"step build did not complete: {error}"
    assert QUOTA_ERROR.search(reason) and not NON_QUOTA.search(reason)


def test_other_codex_error_infos_stay_generic():
    from garuda.acp.protocol import AcpUsageLimitError, remote_error

    error = remote_error(-32603, "Internal error",
                         {"message": "Rate limited", "codexErrorInfo": "rateLimitExceeded"})
    assert not isinstance(error, AcpUsageLimitError)


# --- remote error data is redacted (#729); a disk quota is not a usage limit (#728) ---


_DATA_TOKEN = "sk-live-" + "abcdef1234567890"


@pytest.mark.parametrize("data", [
    {"message": f"upstream 401: Authorization: Bearer {_DATA_TOKEN} rejected"},
    {"details": f"request echo api_key={_DATA_TOKEN}"},
    f"upstream said Bearer {_DATA_TOKEN}",
    {"message": f"usage limit reached for key {_DATA_TOKEN}"},
])
def test_a_remote_error_never_carries_a_secret_from_its_data(data):
    from garuda.acp.protocol import remote_error
    from garuda.core.failure_evidence import classify_exception

    error = remote_error(-32603, f"Internal error ({_DATA_TOKEN})", data)
    assert _DATA_TOKEN not in str(error) and _DATA_TOKEN not in error.detail
    assert "[REDACTED:" in str(error)
    assert _DATA_TOKEN not in repr(classify_exception(error))


@pytest.mark.parametrize("data", [
    {"message": "failed to write session file: Disk quota exceeded (os error 122)"},
    "Disk quota exceeded",
    {"details": "write rollout: quota exceeded (os error 122)"},
    {"message": "EDQUOT: quota exceeded"},
])
def test_an_os_disk_quota_error_is_not_a_usage_limit(data):
    from garuda.acp.protocol import AcpUsageLimitError, remote_error
    from garuda.core.failure_evidence import classify_exception

    error = remote_error(-32603, "Internal error", data)
    assert not isinstance(error, AcpUsageLimitError)
    assert "harness.limit_reached" not in str(error)
    assert classify_exception(error)[0] != "harness.limit_reached"


def test_the_structured_usage_limit_signal_still_wins_over_disk_text():
    from garuda.acp.protocol import AcpUsageLimitError, remote_error

    error = remote_error(-32603, "Internal error", {
        "message": "Disk quota exceeded", "codexErrorInfo": "usageLimitExceeded"})
    assert isinstance(error, AcpUsageLimitError)
    assert isinstance(remote_error(-32603, "Internal error",
                                   {"message": "API quota exceeded for this plan"}),
                      AcpUsageLimitError)
