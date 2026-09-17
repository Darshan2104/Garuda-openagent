"""The inspector's read model: the cached reader, the event window, and the threshold.

Step 4's UI is untested by design (it is JS, checked by executing the page), so these
tests cover the three server-side pieces it depends on — and the two of them that could
be silently wrong: a shared reader that double-counts events, and a compaction threshold
copied instead of derived.
"""

import json
import threading

import pytest

from garuda.core.events import EventStore, EventType
from garuda.core.modes import GATE_FIELDS
from garuda.core.sessions import SessionStore
from garuda.interfaces.web import reads
from garuda.interfaces.web.routes import DashboardContext, dispatch
from garuda.interfaces.web.security import TOKEN_HEADER
from garuda.interfaces.web.wire import Request
from garuda.types import AgentResult

PORT = 8787
TOKEN = "test-token-value"


@pytest.fixture
def store(tmp_path):
    return SessionStore(root=tmp_path / "sessions")


@pytest.fixture
def ctx(store):
    return DashboardContext(port=PORT, token=TOKEN, store=store)


def call(ctx, path, query=""):
    from urllib.parse import parse_qs

    return dispatch(
        Request(
            method="GET",
            path=path,
            query=parse_qs(query),
            headers={"host": f"127.0.0.1:{PORT}", TOKEN_HEADER: TOKEN},
        ),
        ctx,
    )


def body(response):
    return json.loads(response.body)


def _events(store, session_id, records):
    path = store.events_path(session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(record) for record in records) + "\n", encoding="utf-8")
    return path


def _turn(number, *, at="2026-08-05T12:00:0"):
    """One complete turn: budget snapshot, response, metrics."""
    return [
        {"type": "budget", "timestamp": f"{at}0+00:00",
         "payload": {"stage": "context", "turn": number, "used_tokens": 10 * number,
                     "capacity_tokens": 100, "fraction": 0.1 * number}},
        {"type": "model_response", "timestamp": f"{at}1+00:00",
         "payload": {"turn": number, "content": f"turn {number}", "tool_calls": [], "usage": {}}},
        {"type": "turn_metrics", "timestamp": f"{at}2+00:00",
         "payload": {"turn": number, "model_ms": 10.0}},
    ]


def _run(store, session_id, records, *, condenser="microcompact"):
    store.begin(session_id=session_id, task="t", model="openrouter/x/y", agent="build",
                workspace="/tmp")
    store.finish(session_id, AgentResult(success=True, final_message="done", messages=[], turns=1))
    return _events(
        store,
        session_id,
        [
            {"type": "session_start", "timestamp": "2026-08-05T11:59:59+00:00",
             "payload": {"task": "t", "model": "openrouter/x/y", "mode": "eval",
                         "config": {**dict.fromkeys(GATE_FIELDS, True), "condenser": condenser}}},
            *records,
        ],
    )


# --- the reader cache --------------------------------------------------------


def test_the_cache_reuses_one_reader_and_reports_a_stable_count(store):
    path = _run(store, "cached1", _turn(1))
    cache = reads.ReaderCache()
    directory = path.parent

    first_run, first_count = cache.read(directory)
    second_run, second_count = cache.read(directory)

    assert first_count == second_count == 4
    # Nothing was appended, so the reader hands back the identical object — which is
    # also the cheapest possible change check for the poller in step 6.
    assert first_run is second_run
    assert len(cache) == 1


def test_the_cache_picks_up_appended_events_without_rereading_the_file(store):
    path = _run(store, "growing1", _turn(1))
    cache = reads.ReaderCache()
    directory = path.parent

    _, before = cache.read(directory)
    with path.open("a", encoding="utf-8") as handle:
        for record in _turn(2):
            handle.write(json.dumps(record) + "\n")
    run, after = cache.read(directory)

    assert (before, after) == (4, 7)
    assert run.turn_count == 2


def test_concurrent_reads_of_one_run_do_not_double_count_events(store):
    """The reason the lock spans `refresh()`, not just the dict lookup.

    A reader reads its byte tail and *then* extends its own event list. Two threads
    interleaving those steps append the same events twice, which does not raise — it
    silently doubles every turn, and a doubled trajectory is worse than a failed one
    because it is read as a real run.

    The reproduction is measured, not assumed: with the lock replaced by a no-op, 12
    threads over a 40-turn log produced a wrong count in 11 of 30 trials. So this is a
    real race with a real detection rate, and the shape here mirrors it.
    """
    turns = [event for number in range(1, 21) for event in _turn(number)]
    path = _run(store, "threaded1", turns)
    cache = reads.ReaderCache()
    directory = path.parent
    counts: list[int] = []
    barrier = threading.Barrier(12)

    def read_once():
        barrier.wait()
        counts.append(cache.read(directory)[1])

    threads = [threading.Thread(target=read_once) for _ in range(12)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert counts and set(counts) == {61}, sorted(set(counts))
    assert cache.read(directory)[0].turn_count == 20


def test_the_cache_evicts_the_least_recently_used_reader(store):
    cache = reads.ReaderCache(capacity=2)
    directories = []
    for index in range(3):
        directories.append(_run(store, f"lru{index}", _turn(1)).parent)
        cache.read(directories[-1])
    assert len(cache) == 2
    # The oldest went first, so re-reading it is a fresh parse — same answer, which is
    # the property that matters: eviction is invisible except in cost.
    assert cache.read(directories[0])[1] == 4


def test_a_truncated_log_is_reread_rather_than_appended_to(store):
    """Harbor rewrites a trial's events.jsonl wholesale on a re-run, so the file
    shrinks. `EventTail` owns that detection; this pins that the cache inherits it
    instead of accumulating both generations."""
    path = _run(store, "rewritten1", [*_turn(1), *_turn(2)])
    cache = reads.ReaderCache()
    assert cache.read(path.parent)[1] == 7

    _run(store, "rewritten1", _turn(1))
    run, count = cache.read(path.parent)
    assert count == 4
    assert run.turn_count == 1


def test_the_detail_route_shares_the_context_cache(ctx, store):
    _run(store, "shared1", _turn(1))
    assert body(call(ctx, "/api/runs/shared1"))["event_count"] == 4
    assert len(ctx.readers) == 1
    # A second request must not build a second reader for the same run.
    body(call(ctx, "/api/runs/shared1"))
    assert len(ctx.readers) == 1


# --- the raw-event window ----------------------------------------------------


def test_the_event_window_serves_a_slice_addressed_by_index(ctx, store):
    _run(store, "window1", [*_turn(1), *_turn(2), *_turn(3)])
    # Index 0 is session_start, so turn 2's three events are 4, 5 and 6.
    payload = body(call(ctx, "/api/runs/window1/events", query="start=4&limit=3"))
    assert payload["start"] == 4
    assert payload["count"] == 3
    assert payload["total"] == 10
    assert [event["type"] for event in payload["events"]] == [
        "budget", "model_response", "turn_metrics"
    ]
    assert {event["payload"]["turn"] for event in payload["events"]} == {2}


def test_the_window_indices_line_up_with_the_turn_cross_references(ctx, store):
    """`Turn.events` is a list of positions, so "show the raw events behind this turn"
    is a slice — and only if both routes agree on what index 0 means."""
    _run(store, "window2", [*_turn(1), *_turn(2)])
    trajectory = body(call(ctx, "/api/runs/window2"))["trajectory"]
    indices = trajectory["turns"][1]["events"]

    payload = body(
        call(ctx, "/api/runs/window2/events", query=f"start={indices[0]}&limit={len(indices)}")
    )
    assert [event["payload"].get("turn") for event in payload["events"]] == [2] * len(indices)


def test_the_window_is_capped_and_clamped(ctx, store):
    _run(store, "window3", _turn(1))
    payload = body(call(ctx, "/api/runs/window3/events", query="limit=999999"))
    assert payload["limit"] == reads.MAX_EVENT_WINDOW
    # A negative start is a typo, not a request to be interpreted — answered as a 400
    # rather than clamped, so a broken client hears about it.
    assert call(ctx, "/api/runs/window3/events", query="start=-5").status == 400
    # Past the end is an empty window, not an error: the client pages until count is 0.
    assert body(call(ctx, "/api/runs/window3/events", query="start=900"))["count"] == 0


def test_a_bad_window_parameter_is_a_400_and_an_unknown_session_a_404(ctx, store):
    _run(store, "window4", _turn(1))
    assert call(ctx, "/api/runs/window4/events", query="start=abc").status == 400
    assert call(ctx, "/api/runs/window4/events", query="limit=abc").status == 400
    assert call(ctx, "/api/runs/nosuch/events").status == 404


@pytest.mark.parametrize("sid", ["..", "../../etc/passwd", "a b"])
def test_a_hostile_session_id_on_the_window_route_is_refused(ctx, sid):
    assert call(ctx, f"/api/runs/{sid}/events").status in (400, 404)


def test_the_window_reads_a_real_event_store(store, tmp_path):
    """Written by `EventStore.attach_persistence`, not by hand — the format the reader
    must actually accept is the one the harness writes."""
    session_id = "real1"
    store.begin(session_id=session_id, task="t", model="m", agent="build", workspace="/tmp")
    events = EventStore(session_id=session_id)
    events.attach_persistence(store.events_path(session_id))
    events.append(EventType.SESSION_START, {"task": "t", "model": "m", "mode": "eval"})
    events.append(EventType.MODEL_RESPONSE, {"turn": 1, "content": "hi", "tool_calls": [],
                                             "usage": {"prompt_tokens": 5}})
    events.append(EventType.TURN_METRICS, {"turn": 1, "model_ms": 4.0})

    payload = reads.read_events(store, session_id)
    assert payload["total"] == 3
    assert [event["type"] for event in payload["events"]] == [
        "session_start", "model_response", "turn_metrics"
    ]
    # The envelope the reader indexes into is the real one, session_id included.
    assert payload["events"][0]["session_id"] == session_id


# --- the compaction threshold -----------------------------------------------


def test_the_threshold_comes_from_the_condenser_the_run_used(ctx, store):
    """Not a constant in the frontend. `microcompact` fires at 0.75 and
    `recent_window` at 0.85, so one hardcoded line would be wrong on most runs — and a
    threshold drawn in the wrong place is read as the reason a compaction did or did
    not happen."""
    _run(store, "cond1", _turn(1), condenser="microcompact")
    _run(store, "cond2", _turn(1), condenser="recent_window")

    assert body(call(ctx, "/api/runs/cond1"))["condenser_threshold"] == 0.75
    assert body(call(ctx, "/api/runs/cond2"))["condenser_threshold"] == 0.85


def test_the_thresholds_match_the_condensers_themselves(store):
    """The anti-drift assertion: read the numbers off the classes, so changing a
    default in `context/condenser.py` fails here rather than mislabelling a chart."""
    from garuda.context.condenser import MicrocompactCondenser, RecentWindowCondenser

    assert reads.condenser_threshold("microcompact") == MicrocompactCondenser().microcompact_fraction
    assert reads.condenser_threshold("recent_window") == RecentWindowCondenser().trigger_fraction


@pytest.mark.parametrize("name", [None, "", "summarizing", "no_such_strategy"])
def test_a_strategy_with_no_trigger_fraction_gets_no_threshold(name):
    """`summarizing` compacts when asked rather than at a fraction, and an unknown name
    is a log from a newer build. Both draw no line — an absent threshold is honest, an
    invented one is not."""
    assert reads.condenser_threshold(name) is None


def test_an_old_log_without_a_config_reports_no_threshold(ctx, store):
    store.begin(session_id="old1", task="t", model="m", agent="build", workspace="/tmp")
    _events(
        store,
        "old1",
        [{"type": "session_start", "timestamp": "2026-08-05T12:00:00+00:00",
          "payload": {"task": "t", "model": "m"}}, *_turn(1)],
    )
    payload = body(call(ctx, "/api/runs/old1"))
    assert payload["condenser_threshold"] is None
    # And the gate stack stays None rather than defaulting to "all off", which the UI
    # renders as "not recorded" — a gate being off must not be guessed.
    assert payload["trajectory"]["gate_stack"] is None


# --- liveness is the log's mtime, not meta.json's status ---------------------


def _running_session(store, session_id, *, age_seconds):
    """A session whose meta says `running` and whose log was last written `age_seconds` ago."""
    import os
    import time as _time

    store.begin(session_id=session_id, task="in flight", model="m", agent="build",
                workspace="/tmp")
    path = _events(store, session_id, [
        {"type": "session_start", "timestamp": "2026-08-06T10:00:00+00:00",
         "payload": {"task": "in flight", "model": "m"}},
        *_turn(1),
    ])
    when = _time.time() - age_seconds
    os.utime(path, (when, when))
    return path


def test_a_freshly_written_running_session_is_live(ctx, store):
    _running_session(store, "livenow", age_seconds=1)
    row = body(call(ctx, "/api/runs"))["runs"][0]
    assert row["status"] == "running"
    assert row["live"] is True
    assert row["stale"] is False


def test_a_killed_run_claims_running_forever_and_is_not_live(ctx, store):
    """The bug this exists for. `meta.json` says `running` until a run *finishes writing it*,
    so Ctrl-C, a crash or a closed terminal leaves it saying `running` permanently. Four
    month-old sessions on the author's machine did exactly that, and a client that trusted
    the status re-rendered the whole page every 2.5 seconds until the tab was closed."""
    _running_session(store, "abandoned1", age_seconds=60 * 60 * 24 * 30)
    row = body(call(ctx, "/api/runs"))["runs"][0]
    assert row["status"] == "running", "the status itself is left alone"
    assert row["live"] is False
    assert row["stale"] is True
    # Reported so the UI can say how long ago rather than just "not live".
    assert row["written_ago_seconds"] > reads.LIVE_WINDOW_SECONDS


def test_a_running_session_with_no_log_at_all_is_not_live(ctx, store):
    """There is no evidence it is doing anything, so it must not be polled."""
    store.begin(session_id="nolog1", task="t", model="m", agent="build", workspace="/tmp")
    row = body(call(ctx, "/api/runs"))["runs"][0]
    assert row["live"] is False
    assert row["stale"] is True
    assert row["written_ago_seconds"] is None


def test_a_finished_session_is_never_live_or_stale(ctx, store):
    _run(store, "done1", _turn(1))
    row = body(call(ctx, "/api/runs"))["runs"][0]
    assert row["status"] == "success"
    assert (row["live"], row["stale"]) == (False, False)


def test_the_tail_route_uses_the_same_liveness_rule(ctx, store):
    """Both halves matter: the list decides whether to offer a live link, and the tail
    decides whether the client keeps polling. Disagreeing would mean a poller that starts
    and never stops."""
    _running_session(store, "stale2", age_seconds=60 * 60 * 24)
    payload = body(call(ctx, "/api/runs/stale2/tail"))
    assert payload["status"] == "running"
    assert payload["running"] is False, "a quiet log must stop the client's poll"
    assert payload["stale"] is True

    _running_session(store, "fresh2", age_seconds=1)
    fresh = body(call(ctx, "/api/runs/fresh2/tail"))
    assert fresh["running"] is True
    assert fresh["stale"] is False


def test_the_live_window_is_generous_enough_for_a_slow_turn(ctx, store):
    """A turn can spend a while in one bash command without writing an event, so the window
    has to outlast that — otherwise a genuinely live run would be declared abandoned
    mid-turn, which is the opposite failure."""
    assert reads.LIVE_WINDOW_SECONDS >= 60
    _running_session(store, "slowturn", age_seconds=reads.LIVE_WINDOW_SECONDS - 10)
    assert body(call(ctx, "/api/runs"))["runs"][0]["live"] is True
