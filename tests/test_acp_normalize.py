"""Normalizer golden-stream tests for issue #23 (P0.14).

Normal, partial, out-of-order, and terminal/error streams: ordering and
terminal-state rules hold on every one, partials survive exactly once, and raw
records stay redacted diagnostics.
"""

import pytest

from garuda.acp.normalize import AcpNormalizer
from garuda.acp.protocol import AcpProtocolError
from garuda.runtime.events import RuntimeEventKind


def _normalizer() -> AcpNormalizer:
    normalizer = AcpNormalizer("n1")
    normalizer.new_turn()
    return normalizer


def _kinds(events) -> list:
    return [e.kind for e in events]


def test_normal_stream_golden():
    normalizer = _normalizer()
    events = []
    events += normalizer.feed({"updateType": "agent_message_chunk", "text": "Working on it."})
    events += normalizer.feed(
        {"updateType": "tool_call", "toolCallId": "c1", "title": "Write a.py", "kind": "edit"}
    )
    events += normalizer.feed(
        {"updateType": "tool_call_update", "toolCallId": "c1", "status": "done", "content": "ok"}
    )
    events += normalizer.feed(
        {"updateType": "diff", "path": "a.py", "oldText": "", "newText": "x = 1"}
    )
    events += normalizer.finish("end_turn")
    assert _kinds(events) == [
        RuntimeEventKind.MESSAGE,
        RuntimeEventKind.TOOL_CALL,
        RuntimeEventKind.TOOL_RESULT,
        RuntimeEventKind.TOOL_RESULT,
        RuntimeEventKind.LIFECYCLE,
    ]
    assert [e.seq for e in events] == [0, 1, 2, 3, 4]
    assert all(e.session_id == "n1" and e.turn == 1 for e in events)
    assert events[-1].payload["state"] == "completed"
    # The turn is closed: feeding needs a new turn, which reopens cleanly.
    with pytest.raises(AcpProtocolError):
        normalizer.feed({"updateType": "agent_message_chunk", "text": "late"})
    assert normalizer.new_turn() == 2
    reopened = normalizer.feed({"updateType": "agent_message_chunk", "text": "again"})
    assert reopened[0].turn == 2


def test_partial_chunks_preserved_without_duplicate_final():
    normalizer = _normalizer()
    events = []
    for chunk in ("hel", "lo, ", "world"):
        events += normalizer.feed({"updateType": "agent_message_chunk", "text": chunk})
    events += normalizer.finish("completed")
    messages = [e for e in events if e.kind is RuntimeEventKind.MESSAGE]
    assert "".join(m.payload["chunk"] for m in messages) == "hello, world"
    assert not [e for e in messages if e.payload.get("chunk") == "hello, world"]


def test_out_of_order_update_waits_for_its_call():
    normalizer = _normalizer()
    early = normalizer.feed(
        {"updateType": "tool_call_update", "toolCallId": "c9", "status": "done"}
    )
    assert early == []
    events = normalizer.feed(
        {"updateType": "tool_call", "toolCallId": "c9", "title": "Late call"}
    )
    assert _kinds(events) == [RuntimeEventKind.TOOL_CALL, RuntimeEventKind.TOOL_RESULT]
    assert events[0].payload["tool_call_id"] == "c9"
    assert events[1].payload["tool_call_id"] == "c9"


def test_orphan_updates_and_duplicate_calls_fail_closed():
    normalizer = _normalizer()
    normalizer.feed(
        {"updateType": "tool_call_update", "toolCallId": "missing", "status": "done"}
    )
    with pytest.raises(AcpProtocolError, match="unknown tool calls"):
        normalizer.finish("completed")

    normalizer = _normalizer()
    call = {"updateType": "tool_call", "toolCallId": "c1", "title": "one"}
    normalizer.feed(call)
    with pytest.raises(AcpProtocolError, match="duplicate"):
        normalizer.feed(call)


def test_terminal_error_stream_golden():
    normalizer = _normalizer()
    events = normalizer.feed({"updateType": "error", "message": "agent blew up"})
    events += normalizer.finish("error", detail="agent blew up")
    assert _kinds(events) == [RuntimeEventKind.ERROR, RuntimeEventKind.ERROR, RuntimeEventKind.LIFECYCLE]
    assert events[-1].payload["state"] == "failed"
    assert events[-1].is_terminal()
    with pytest.raises(AcpProtocolError):
        normalizer.feed({"updateType": "agent_message_chunk", "text": "late"})


def test_approval_and_unknown_kinds():
    normalizer = _normalizer()
    events = normalizer.feed(
        {"updateType": "approval_request", "approvalId": "a1", "action": "rm -rf /"}
    )
    assert events[0].kind is RuntimeEventKind.APPROVAL_REQUEST
    assert events[0].payload["approval_id"] == "a1"
    exotic = normalizer.feed({"updateType": "telepathy", "waves": [1]})
    assert exotic[0].kind is RuntimeEventKind.MESSAGE
    with pytest.raises(AcpProtocolError):
        normalizer.feed({"updateType": "tool_call"})
    with pytest.raises(AcpProtocolError):
        normalizer.finish("mystery")


def test_diagnostics_are_redacted_and_scoped():
    normalizer = _normalizer()
    normalizer.feed({"updateType": "agent_message_chunk", "text": "token=supersecret1"})
    pem = (
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "MIIEpAIBAAKCAQEA7bq3fakekeybody\n"
        "-----END RSA PRIVATE KEY-----"
    )
    normalizer.feed({"updateType": "agent_message_chunk", "text": pem})
    trail = normalizer.diagnostic_trail()
    assert len(trail) == 2
    assert "supersecret1" not in trail[0]["raw"]
    assert "REDACTED" in trail[0]["raw"]
    # Full PEM blocks (not just headers) are scrubbed from diagnostics.
    assert "MIIEpAIBAAKCAQEA7bq3fakekeybody" not in trail[1]["raw"]
    assert "BEGIN RSA PRIVATE KEY" not in trail[1]["raw"]
