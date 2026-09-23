"""Byte-offset tailing of a growing events.jsonl.

Driven against a real ``EventStore`` with ``attach_persistence`` wherever possible, because
the properties that matter are properties of the actual writer: it opens, writes and closes
per event, so an append is not atomic and a poll can land mid-write.
"""

import json

import pytest

from garuda.core.events import EventStore, EventType
from garuda.core.sessions import SessionStore
from garuda.interfaces.web.routes import DashboardContext, dispatch
from garuda.interfaces.web.security import TOKEN_HEADER
from garuda.interfaces.web.tail import MAX_TAIL_BYTES, tail_jsonl
from garuda.interfaces.web.wire import Request
from garuda.types import AgentResult

PORT = 8787
TOKEN = "test-token-value"


@pytest.fixture
def store(tmp_path):
    return SessionStore(root=tmp_path / "sessions")


@pytest.fixture
def live(store):
    """A session being written by a real EventStore, exactly as a run does it."""
    session_id = "live1"
    store.begin(session_id=session_id, task="t", model="m", agent="build", workspace="/tmp")
    events = EventStore(session_id=session_id)
    events.attach_persistence(store.events_path(session_id))
    return session_id, events, store.events_path(session_id)


@pytest.fixture
def ctx(store):
    return DashboardContext(port=PORT, token=TOKEN, store=store)


def call(ctx, path, query=""):
    from urllib.parse import parse_qs

    return dispatch(
        Request(method="GET", path=path, query=parse_qs(query),
                headers={"host": f"127.0.0.1:{PORT}", TOKEN_HEADER: TOKEN}),
        ctx,
    )


def body(response):
    return json.loads(response.body)


# --- the cursor --------------------------------------------------------------


def test_sequential_polls_deliver_each_event_exactly_once(live):
    """The core contract. Offsets are monotonic and no event is repeated or skipped across
    polls, which is what lets the client append rather than reconcile."""
    _, events, path = live
    seen = []
    offset = 0
    for index in range(5):
        events.append(EventType.MODEL_RESPONSE, {"turn": index, "content": f"n{index}"})
        result = tail_jsonl(path, offset)
        assert result.offset >= offset
        assert result.eof is True
        offset = result.offset
        seen.extend(event["payload"]["turn"] for event in result.events)

    assert seen == [0, 1, 2, 3, 4]
    # And a poll with nothing new is empty rather than a repeat.
    assert tail_jsonl(path, offset).events == []


def test_a_half_written_event_is_held_back_and_delivered_whole(live):
    """`EventStore.append` opens, writes and closes per event, so a poll genuinely can land
    mid-write. Consuming a partial line would leave the cursor inside a JSON object and
    desync every later read — permanently, not just for that poll."""
    _, events, path = live
    events.append(EventType.MODEL_RESPONSE, {"turn": 1, "content": "complete"})
    first = tail_jsonl(path, 0)
    assert len(first.events) == 1

    # Simulate the interrupted write: a record with no trailing newline.
    fragment = json.dumps({"type": "model_response", "payload": {"turn": 2, "content": "half"}})
    with path.open("a", encoding="utf-8") as handle:
        handle.write(fragment[: len(fragment) // 2])

    torn = tail_jsonl(path, first.offset)
    assert torn.events == []
    assert torn.offset == first.offset, "the cursor must not advance into a partial line"
    assert torn.eof is False, "there are bytes pending, so this is not the end of the file"

    # The writer finishes the line.
    with path.open("a", encoding="utf-8") as handle:
        handle.write(fragment[len(fragment) // 2 :] + "\n")

    healed = tail_jsonl(path, first.offset)
    assert [event["payload"]["turn"] for event in healed.events] == [2]
    assert healed.eof is True


def test_a_multibyte_character_split_across_polls_arrives_intact(store):
    """A byte cursor can land inside a UTF-8 sequence. Garuda's own logs are pure ASCII
    (`json.dumps` defaults to `ensure_ascii=True`), so this needs an explicit non-ASCII
    write to test at all — but a task statement echoed by another writer could contain one,
    and half a code point is a decode error, not a missing character."""
    path = store.events_path("mb1")
    path.parent.mkdir(parents=True, exist_ok=True)
    record = json.dumps({"type": "user_message", "payload": {"content": "café 🚀 done"}},
                        ensure_ascii=False)
    encoded = record.encode("utf-8") + b"\n"
    # Cut inside the 4-byte emoji.
    split = encoded.index("🚀".encode()) + 2

    path.write_bytes(encoded[:split])
    first = tail_jsonl(path, 0)
    assert first.events == []
    assert first.offset == 0

    with path.open("ab") as handle:
        handle.write(encoded[split:])
    second = tail_jsonl(path, 0)
    assert second.events[0]["payload"]["content"] == "café 🚀 done"


def test_a_truncated_file_resets_the_cursor_to_zero(live):
    """Harbor's `events.save()` rewrites a trial's log wholesale, so a re-run shrinks the
    file. Seeking past the end would report a quiet, permanent EOF instead."""
    _, events, path = live
    for index in range(4):
        events.append(EventType.MODEL_RESPONSE, {"turn": index})
    first = tail_jsonl(path, 0)
    assert len(first.events) == 4

    path.write_text(json.dumps({"type": "session_start", "payload": {"task": "again"}}) + "\n",
                    encoding="utf-8")
    reset = tail_jsonl(path, first.offset)
    assert reset.truncated is True
    assert reset.offset == 0
    assert reset.events == []

    # And the client's next poll from 0 gets the new generation.
    fresh = tail_jsonl(path, 0)
    assert fresh.truncated is False
    assert [event["type"] for event in fresh.events] == ["session_start"]


def test_a_malformed_complete_line_is_skipped_and_counted(store):
    """Skipped so one bad line does not stop the stream, counted so a corrupt log does not
    look like a quiet one."""
    path = store.events_path("bad1")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"type": "a", "payload": {}}) + "\n"
        + "{ not json at all\n"
        + "[1, 2, 3]\n"                       # valid JSON, wrong shape
        + "\n"                                # blank: skipped silently
        + json.dumps({"type": "b", "payload": {}}) + "\n",
        encoding="utf-8",
    )
    result = tail_jsonl(path, 0)
    assert [event["type"] for event in result.events] == ["a", "b"]
    assert result.malformed == 2
    assert result.eof is True


def test_a_burst_larger_than_the_cap_is_delivered_over_several_polls(store):
    path = store.events_path("big1")
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps({"type": "tool_result", "payload": {"content": "x" * 900}}) + "\n"
    count = (MAX_TAIL_BYTES // len(line)) + 40
    path.write_text(line * count, encoding="utf-8")

    first = tail_jsonl(path, 0)
    assert first.eof is False, "the cap was not reached, so this test proves nothing"
    assert len(first.events) < count
    # The client polls again immediately on `eof: false` rather than waiting out its
    # interval, so the rest arrives without a stall.
    total = len(first.events)
    offset = first.offset
    while True:
        page = tail_jsonl(path, offset)
        total += len(page.events)
        offset = page.offset
        if page.eof:
            break
    assert total == count


def test_a_missing_file_is_reported_not_raised(store):
    """A session directory exists before its first event is written, and a poller that
    starts with the run must not fail on that gap."""
    result = tail_jsonl(store.events_path("nofile"), 0)
    assert result.missing is True
    assert result.events == []
    assert result.size == 0


def test_an_empty_file_is_eof_not_missing(store):
    path = store.events_path("empty1")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"")
    result = tail_jsonl(path, 0)
    assert (result.missing, result.eof, result.events) == (False, True, [])


def test_a_negative_offset_is_clamped(live):
    _, events, path = live
    events.append(EventType.SESSION_START, {"task": "t"})
    assert len(tail_jsonl(path, -500).events) == 1


# --- the route ---------------------------------------------------------------


def test_the_tail_route_carries_the_status_in_the_same_response(ctx, store, live):
    """One response cannot disagree with itself. Two requests — tail and status — can
    interleave so the client sees `finished` and then a batch of events it decides not to
    render, or `running` forever because the status read landed first."""
    session_id, events, _ = live
    events.append(EventType.SESSION_START, {"task": "t"})
    payload = body(call(ctx, f"/api/runs/{session_id}/tail"))
    assert payload["running"] is True
    assert payload["status"] == "running"
    assert len(payload["events"]) == 1
    assert payload["offset"] > 0

    store.finish(session_id, AgentResult(success=True, final_message="done", messages=[], turns=1))
    after = body(call(ctx, f"/api/runs/{session_id}/tail", query=f"offset={payload['offset']}"))
    assert after["running"] is False
    assert after["status"] == "success"
    assert after["events"] == []


def test_the_client_stop_condition_needs_both_halves(ctx, store, live):
    """A finished run whose last events have not been consumed yet must keep polling, so
    the stop condition is `eof AND not running` — never `not running` alone."""
    session_id, events, _ = live
    store.finish(session_id, AgentResult(success=True, final_message="done", messages=[], turns=1))
    for index in range(3):
        events.append(EventType.MODEL_RESPONSE, {"turn": index})

    payload = body(call(ctx, f"/api/runs/{session_id}/tail"))
    assert payload["running"] is False
    assert len(payload["events"]) == 3, "events written after finish must still be delivered"


def test_the_tail_route_validates_its_offset(ctx, live):
    session_id, _, _ = live
    assert call(ctx, f"/api/runs/{session_id}/tail", query="offset=abc").status == 400
    assert call(ctx, f"/api/runs/{session_id}/tail", query="offset=-1").status == 400
    assert call(ctx, "/api/runs/nosuchsession/tail").status == 404


@pytest.mark.parametrize("sid", ["..", "../../etc/passwd", "a b"])
def test_a_hostile_session_id_on_the_tail_route_is_refused(ctx, sid):
    assert call(ctx, f"/api/runs/{sid}/tail").status in (400, 404)


def test_the_tail_and_events_routes_use_different_cursors(ctx, store, live):
    """`/tail` is byte-addressed and `/events` index-addressed, and mixing them up would be
    a silent misread — offset 4 in bytes is the middle of the first line, while index 4 is
    the fifth event. Different names, different meanings, checked."""
    session_id, events, _ = live
    for index in range(5):
        events.append(EventType.MODEL_RESPONSE, {"turn": index})

    tail = body(call(ctx, f"/api/runs/{session_id}/tail"))
    window = body(call(ctx, f"/api/runs/{session_id}/events", query="start=4&limit=1"))

    assert tail["offset"] > 100, "a byte offset over five records is large"
    assert window["start"] == 4
    assert window["events"][0]["payload"]["turn"] == 4
    assert tail["size"] == tail["offset"]
