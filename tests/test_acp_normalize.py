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


def _chunk(text: str) -> dict:
    return {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": text}}


def _kinds(events) -> list:
    return [e.kind for e in events]


def test_normal_stream_golden():
    normalizer = _normalizer()
    events = []
    events += normalizer.feed(_chunk("Working on it."))
    events += normalizer.feed(
        {"sessionUpdate": "tool_call", "toolCallId": "c1", "title": "Write a.py", "kind": "edit"}
    )
    events += normalizer.feed(
        {
            "sessionUpdate": "tool_call_update",
            "toolCallId": "c1",
            "status": "completed",
            "content": [
                {"type": "content", "content": {"type": "text", "text": "ok"}},
                {"type": "diff", "path": "a.py", "oldText": None, "newText": "x = 1"},
            ],
        }
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
        normalizer.feed(_chunk("late"))
    assert normalizer.new_turn() == 2
    reopened = normalizer.feed(_chunk("again"))
    assert reopened[0].turn == 2


def test_partial_chunks_preserved_without_duplicate_final():
    normalizer = _normalizer()
    events = []
    for chunk in ("hel", "lo, ", "world"):
        events += normalizer.feed(_chunk(chunk))
    events += normalizer.finish("completed")
    messages = [e for e in events if e.kind is RuntimeEventKind.MESSAGE]
    assert "".join(m.payload["chunk"] for m in messages) == "hello, world"
    assert not [e for e in messages if e.payload.get("chunk") == "hello, world"]


def test_out_of_order_update_waits_for_its_call():
    normalizer = _normalizer()
    early = normalizer.feed(
        {"sessionUpdate": "tool_call_update", "toolCallId": "c9", "status": "done"}
    )
    assert early == []
    events = normalizer.feed(
        {"sessionUpdate": "tool_call", "toolCallId": "c9", "title": "Late call"}
    )
    assert _kinds(events) == [RuntimeEventKind.TOOL_CALL, RuntimeEventKind.TOOL_RESULT]
    assert events[0].payload["tool_call_id"] == "c9"
    assert events[1].payload["tool_call_id"] == "c9"


def test_orphan_updates_and_duplicate_calls_fail_closed():
    normalizer = _normalizer()
    normalizer.feed(
        {"sessionUpdate": "tool_call_update", "toolCallId": "missing", "status": "done"}
    )
    with pytest.raises(AcpProtocolError, match="unknown tool calls"):
        normalizer.finish("completed")

    normalizer = _normalizer()
    call = {"sessionUpdate": "tool_call", "toolCallId": "c1", "title": "one"}
    normalizer.feed(call)
    with pytest.raises(AcpProtocolError, match="duplicate"):
        normalizer.feed(call)


def test_terminal_error_stream_golden():
    normalizer = _normalizer()
    events = normalizer.finish("failed", detail="agent blew up")
    assert _kinds(events) == [RuntimeEventKind.ERROR, RuntimeEventKind.LIFECYCLE]
    assert events[-1].payload["state"] == "failed"
    assert events[-1].is_terminal()
    with pytest.raises(AcpProtocolError):
        normalizer.feed(_chunk("late"))


@pytest.mark.parametrize("reason", ["end_turn", "max_tokens", "max_turn_requests", "refusal"])
def test_v1_turn_ending_stop_reasons_keep_the_session_open(reason):
    normalizer = _normalizer()
    events = normalizer.finish(reason)
    assert events[-1].payload == {"state": "completed", "stop_reason": reason}
    assert normalizer.new_turn() == 2


def test_v1_content_blocks_and_unknown_kinds():
    normalizer = _normalizer()
    [thought] = normalizer.feed(
        {"sessionUpdate": "agent_thought_chunk", "content": {"type": "text", "text": "hmm"}}
    )
    assert thought.payload == {"thought": "hmm"}
    [image] = normalizer.feed(
        {"sessionUpdate": "agent_message_chunk", "content": {"type": "image", "data": "..."}}
    )
    assert image.payload == {"chunk": "", "content_type": "image"}
    for kind in ("plan", "available_commands_update", "current_mode_update", "telepathy"):
        [event] = normalizer.feed({"sessionUpdate": kind, "entries": []})
        assert event.kind is RuntimeEventKind.MESSAGE
        assert event.payload["acp_update"] == kind
    with pytest.raises(AcpProtocolError, match="sessionUpdate"):
        normalizer.feed({"updateType": "agent_message_chunk", "text": "pre-v1 shape"})
    with pytest.raises(AcpProtocolError):
        normalizer.feed({"sessionUpdate": "agent_message_chunk", "content": "bare string"})
    with pytest.raises(AcpProtocolError):
        normalizer.feed({"sessionUpdate": "tool_call"})
    with pytest.raises(AcpProtocolError):
        normalizer.finish("mystery")


def test_diagnostics_are_redacted_and_scoped():
    normalizer = _normalizer()
    normalizer.feed(_chunk("token=supersecret1"))
    pem = (
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "MIIEpAIBAAKCAQEA7bq3fakekeybody\n"
        "-----END RSA PRIVATE KEY-----"
    )
    normalizer.feed(_chunk(pem))
    trail = normalizer.diagnostic_trail()
    assert len(trail) == 2
    assert "supersecret1" not in trail[0]["raw"]
    assert "REDACTED" in trail[0]["raw"]
    # Full PEM blocks (not just headers) are scrubbed from diagnostics.
    assert "MIIEpAIBAAKCAQEA7bq3fakekeybody" not in trail[1]["raw"]
    assert "BEGIN RSA PRIVATE KEY" not in trail[1]["raw"]
