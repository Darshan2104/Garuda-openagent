"""Turn-structured reading of a Garuda events.jsonl trajectory.

Every consumer that wants to explain a run — a dashboard, a triage script, a failure
taxonomy — has to rebuild turn structure from a flat append-only log. These tests pin
the rules of that reconstruction, because each one exists to encode a verified fact
about how the harness emits, and a reader that guesses wrong reports a run that never
happened.

The load-bearing ones, if this list is ever trimmed: the disjoint-cover property
(nothing dropped or double-counted), `session_end` not being last, the three
overloaded payload shapes, the id-key asymmetry, and turn attribution surviving on a
log written before `turn` existed.
"""

import json

from garuda.observability.trajectory import (
    EventTail,
    TrajectoryReader,
    build_run,
    find_events_file,
    read_trajectory,
)

# --- fixture builders --------------------------------------------------------
# Hand-built event lists, matching the style at tests/test_atif_dashboard.py:12-35.
# Note those repo fixtures omit `session_id` entirely, so the reader must not need it.

_CLOCK = ["2026-08-05T12:00:00.000000+00:00"]


def _ts(step: int) -> str:
    return f"2026-08-05T12:{step // 60:02d}:{step % 60:02d}.000000+00:00"


def _ev(event_type: str, payload: dict, at: int = 0) -> dict:
    return {"type": event_type, "timestamp": _ts(at), "payload": payload}


def _start(**extra) -> dict:
    payload = {"task": "do it", "model": "openai/gpt-4o-mini"}
    payload.update(extra)
    return _ev("session_start", payload)


def _budget(turn: int, **extra) -> dict:
    payload = {
        "stage": "context",
        "turn": turn,
        "used_tokens": 1000 * turn,
        "capacity_tokens": 100_000,
        "fraction": 0.01 * turn,
    }
    payload.update(extra)
    return _ev("budget", payload)


def _metrics(turn: int, **extra) -> dict:
    payload = {"turn": turn, "model_ms": 100.0, "tool_ms_total": 5.0, "tool_wall_ms": 5.0,
               "tool_calls": 1, "tool_errors": 0, "prompt_tokens": 10, "completion_tokens": 5}
    payload.update(extra)
    return _ev("turn_metrics", payload)


def _response(*, turn=None, calls=(), content=None, usage=None, **extra) -> dict:
    payload = {
        "content": content,
        "tool_calls": [dict(c) for c in calls],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5} if usage is None else usage,
    }
    if turn is not None:
        payload["turn"] = turn
    payload.update(extra)
    return _ev("model_response", payload)


def _spine(call_id: str, name: str, **arguments) -> dict:
    return {"id": call_id, "name": name, "arguments": arguments}


def _call(call_id: str, name: str, *, turn=None, **arguments) -> dict:
    payload = {"id": call_id, "name": name, "arguments": arguments}
    if turn is not None:
        payload["turn"] = turn
    return _ev("tool_call", payload)


def _result(call_id: str, name: str, content="ok", *, is_error=False, turn=None, **extra) -> dict:
    payload = {"tool_call_id": call_id, "name": name, "content": content,
               "is_error": is_error, "duration_ms": 12.5}
    if turn is not None:
        payload["turn"] = turn
    payload.update(extra)
    return _ev("tool_result", payload)


def _read_turn(number: int, *, call_id="r1") -> list[dict]:
    """One canonical turn: preflight, a read, its result, metrics."""
    return [
        _budget(number),
        _response(turn=number, calls=[_spine(call_id, "read_file", path="a.txt")]),
        _call(call_id, "read_file", turn=number, path="a.txt"),
        _result(call_id, "read_file", turn=number),
        _metrics(number),
    ]


def _submit_turn(number: int, *, approved=True, call_id="c1") -> list[dict]:
    return [
        _budget(number),
        _response(turn=number, calls=[
            _spine(call_id, "task_complete", summary="did it", verification_commands=["pytest -q"])
        ]),
        _ev("verification", {"approved": approved, "checklist": {"summary_present": True},
                            "feedback": None if approved else "not enough evidence",
                            "evidence": [{"command": "pytest -q", "exit_code": 0}],
                            "attempt": 1, "turn": number}),
        _metrics(number),
    ]


def _log(*turns: list[dict], end=None, start=None) -> list[dict]:
    events = [start or _start(), _ev("user_message", {"content": "do it"})]
    for turn in turns:
        events.extend(turn)
    if end is not False:
        events.append(end or _ev("session_end", {"success": True, "turns": len(turns)}))
    return events


def _coverage(run) -> list[int]:
    """The sorted union of every event bucket — must equal range(len(events))."""
    seen = list(run.preamble_events) + list(run.trailing_events)
    for turn in run.turns:
        seen.extend(turn.events)
    return sorted(seen)


# --- A. loading and I/O tolerance -------------------------------------------


def test_empty_log_yields_an_empty_run():
    run = build_run([])
    assert run.turns == []
    assert "empty_log" in run.warning_codes


def test_torn_final_line_is_skipped_and_reported(tmp_path):
    path = tmp_path / "events.jsonl"
    path.write_text(
        json.dumps(_start()) + "\n"
        + json.dumps(_ev("user_message", {"content": "do it"})) + "\n"
        + '{"type": "tool_'  # a crash mid-write
    )
    run = read_trajectory(path)
    assert len(run.events) == 2, "both complete events must survive the torn tail"
    assert "torn_tail" in run.warning_codes


def test_blank_lines_are_skipped_silently(tmp_path):
    """Matching EventStore.load, which does not warn about them."""
    path = tmp_path / "events.jsonl"
    path.write_text(json.dumps(_start()) + "\n\n\n")
    run = read_trajectory(path)
    assert len(run.events) == 1
    assert "malformed_line" not in run.warning_codes


def test_malformed_complete_line_is_reported_and_the_rest_survives(tmp_path):
    path = tmp_path / "events.jsonl"
    path.write_text("{not json}\n" + json.dumps(_start()) + "\n")
    run = read_trajectory(path)
    assert len(run.events) == 1
    assert "malformed_line" in run.warning_codes


def test_events_without_a_session_id_are_accepted():
    """The repo's own fixtures build three-key events; requiring session_id would
    reject them."""
    run = build_run(_log(_read_turn(1)))
    assert run.session_id is None
    assert run.turn_count == 1


def test_non_dict_payload_does_not_crash_the_walk():
    events = _log(_read_turn(1))
    events.insert(2, {"type": "budget", "timestamp": _ts(0), "payload": "oops"})
    run = build_run(events)
    assert "payload_not_a_dict" in run.warning_codes
    assert run.turn_count == 1
    assert _coverage(run) == list(range(len(events)))


def test_unknown_event_type_is_tolerated():
    events = _log(_read_turn(1))
    events.insert(2, _ev("cosmic_ray", {}))
    run = build_run(events)
    assert "unknown_event_type" in run.warning_codes
    assert run.turn_count == 1


def test_find_events_file_resolves_all_three_layouts(tmp_path):
    direct = tmp_path / "a.jsonl"
    direct.write_text("")
    assert find_events_file(direct) == direct

    session = tmp_path / "session"
    (session).mkdir()
    (session / "events.jsonl").write_text("")
    assert find_events_file(session) == session / "events.jsonl"

    trial = tmp_path / "trial"
    (trial / "agent").mkdir(parents=True)
    (trial / "agent" / "events.jsonl").write_text("")
    assert find_events_file(trial) == trial / "agent" / "events.jsonl"

    assert find_events_file(tmp_path / "nothing") is None


# --- B. baseline segmentation ------------------------------------------------


def test_preamble_events_are_not_a_turn():
    events = _log(_read_turn(1), start=_start(agent="build", mode="eval"))
    events.insert(2, _ev("environment_snapshot", {"chars": 512}))
    run = build_run(events)
    assert run.turn_count == 1
    assert run.preamble_events == [0, 1, 2]
    assert run.task == "do it"
    assert run.model == "openai/gpt-4o-mini"
    assert run.agent == "build"
    assert run.mode == "eval"
    assert run.environment_snapshot_chars == 512


def test_three_turns_segment_in_order():
    run = build_run(_log(_read_turn(1, call_id="a"), _read_turn(2, call_id="b"),
                         _submit_turn(3)))
    assert [turn.number for turn in run.turns] == [1, 2, 3]
    assert all(turn.finished for turn in run.turns)
    assert all(len(turn.model_calls) == 1 for turn in run.turns)


def test_every_event_lands_in_exactly_one_bucket():
    """The disjoint-cover property: the whole failure mode of a forward-filling
    segmenter is dropping or double-counting an event, and this is what proves
    neither happened."""
    events = _log(
        [
            _budget(1),
            _ev("summarization", {"turn": 1, "duration_ms": 3.0, "strategy": "microcompact",
                                  "action": "prune", "pruned": 2}),
            _budget(1, stage=0.5, elapsed_sec=12.0),
            _response(turn=1, calls=[_spine("w1", "write_file", path="x"),
                                     _spine("d1", "bash", command="rm -rf /")]),
            _ev("permission_ask", {"approved": False, "reason": "denied", "name": "bash",
                                   "id": "d1", "turn": 1}),
            _call("w1", "write_file", turn=1, path="x"),
            _result("w1", "write_file", turn=1),
            _ev("tool_result", {"failure_streak": 3, "steered": True, "turn": 1}),
            _ev("model_response", {"truncated": True, "turn": 1}),
            _ev("environment_unavailable", {"turn": 1, "reason": "container_gone", "detail": "x"}),
            _metrics(1),
        ],
        [
            _budget(2),
            _response(turn=2, calls=[_spine("c1", "task_complete", summary="did it")]),
            _ev("side_effects", {"files_written": ["x"], "processes_started": []}),
            _ev("contract", {"action": "derived", "count": 1, "criteria": {"criteria": []}}),
            _ev("verification", {"approved": True, "checklist": {}, "feedback": None,
                                 "evidence": [], "attempt": 1, "turn": 2}),
            _metrics(2),
        ],
    )
    run = build_run(events)
    assert _coverage(run) == list(range(len(events)))


def test_missing_session_start_leaves_the_model_unknown():
    events = [*_read_turn(1), _ev("session_end", {"success": True, "turns": 1})]
    run = build_run(events)
    assert run.model is None
    assert "no_session_start" in run.warning_codes
    # Unpriceable without a model name, and reported as such rather than as zero.
    assert run.turns[0].model_calls[0].cost_usd is None
    assert run.cost_usd is None
    assert "cost_unavailable" in run.warning_codes


# --- C. impostor filtering ---------------------------------------------------


def test_truncation_marker_is_not_a_model_call():
    events = _log([
        _budget(1),
        _response(turn=1, calls=[_spine("a", "read_file", path="a")]),
        _ev("model_response", {"truncated": True, "turn": 1}),
        _call("a", "read_file", turn=1, path="a"),
        _result("a", "read_file", turn=1),
        _metrics(1),
    ])
    run = build_run(events)
    turn = run.turns[0]
    assert len(turn.model_calls) == 1
    assert turn.model_calls[0].truncated is True
    assert "multiple_model_calls" not in run.warning_codes


def test_failure_streak_marker_is_not_a_tool_step():
    events = _log([
        _budget(1),
        _response(turn=1, calls=[_spine("a", "bash", command="ls")]),
        _call("a", "bash", turn=1, command="ls"),
        _result("a", "bash", turn=1, is_error=True, content="boom"),
        _ev("tool_result", {"failure_streak": 3, "steered": True, "turn": 1}),
        _metrics(1),
    ])
    run = build_run(events)
    turn = run.turns[0]
    assert turn.failure_steer == 3
    assert len(turn.tool_steps) == 1, "the steering marker must not become a tool step"
    assert turn.tool_steps[0].status == "error"
    assert "orphan_tool_event" not in run.warning_codes


def test_completion_and_critic_verifications_route_differently():
    """Two incompatible payload shapes on one event type."""
    run = build_run(_log(_submit_turn(1)))
    gate = run.turns[0].gates[0]
    assert gate.kind == "completion"
    assert gate.checklist == {"summary_present": True}
    assert len(gate.evidence) == 1
    assert gate.summary == "did it"
    assert gate.verification_commands == ["pytest -q"]


# --- D. tool call/result matching -------------------------------------------


def test_result_matches_across_the_id_key_asymmetry():
    """tool_call uses `id`; tool_result uses `tool_call_id`."""
    run = build_run(_log(_read_turn(1)))
    step = run.turns[0].tool_steps[0]
    assert step.status == "ok"
    assert step.call_id == "r1"


def test_a_result_keyed_id_also_matches():
    """The tolerated variant observability/tracing.py already accepts."""
    events = _log([
        _budget(1),
        _response(turn=1, calls=[_spine("a", "read_file", path="a")]),
        _call("a", "read_file", turn=1, path="a"),
        _ev("tool_result", {"id": "a", "name": "read_file", "content": "X", "is_error": False}),
        _metrics(1),
    ])
    run = build_run(events)
    assert run.turns[0].tool_steps[0].status == "ok"
    assert run.turns[0].tool_steps[0].content == "X"


def test_parallel_results_out_of_order_match_by_id():
    """Two same-named calls in one response: without claim-once discipline both
    bind the first result, which is the bug atif_export documents."""
    events = _log([
        _budget(1),
        _response(turn=1, calls=[_spine("x", "read_file", path="x.txt"),
                                 _spine("y", "read_file", path="y.txt")]),
        _call("x", "read_file", turn=1, path="x.txt"),
        _call("y", "read_file", turn=1, path="y.txt"),
        _result("y", "read_file", "Y-CONTENT", turn=1),
        _result("x", "read_file", "X-CONTENT", turn=1),
        _metrics(1),
    ])
    steps = build_run(events).turns[0].tool_steps
    assert [s.call_id for s in steps] == ["x", "y"], "call order is the model's order"
    assert steps[0].content == "X-CONTENT"
    assert steps[1].content == "Y-CONTENT"


def test_tool_duration_comes_from_the_payload_never_timestamps():
    """A parallel batch emits every tool_call at the same instant, so differencing
    timestamps is meaningless — and an absent duration is None, not zero."""
    events = _log([
        _budget(1),
        _response(turn=1, calls=[_spine("a", "bash", command="ls"),
                                 _spine("b", "bash", command="pwd")]),
        _call("a", "bash", turn=1, command="ls"),
        _call("b", "bash", turn=1, command="pwd"),
        _result("a", "bash", turn=1, duration_ms=1234.5),
        _ev("tool_result", {"tool_call_id": "b", "name": "bash", "content": "ok",
                            "is_error": False}),
        _metrics(1),
    ])
    steps = build_run(events).turns[0].tool_steps
    assert steps[0].duration_ms == 1234.5
    assert steps[1].duration_ms is None


def test_unmatched_tool_call_is_pending():
    events = _log([
        _budget(1),
        _response(turn=1, calls=[_spine("a", "bash", command="sleep 999")]),
        _call("a", "bash", turn=1, command="sleep 999"),
    ], end=False)
    run = build_run(events)
    step = run.turns[0].tool_steps[0]
    assert step.status == "pending"
    assert "tool_call_unanswered" in run.warning_codes


def test_a_denied_call_is_attributed_to_its_step():
    """permission_ask carries the id, so the denial lands on the call it blocked
    instead of collapsing into an undifferentiated 'no result'."""
    events = _log([
        _budget(1),
        _response(turn=1, calls=[_spine("w1", "write_file", path="x"),
                                 _spine("ok1", "read_file", path="a")]),
        _ev("permission_ask", {"approved": False, "reason": "Permission denied for write_file",
                               "name": "write_file", "id": "w1", "turn": 1}),
        _call("ok1", "read_file", turn=1, path="a"),
        _result("ok1", "read_file", turn=1),
        _metrics(1),
    ])
    run = build_run(events)
    denied, allowed = run.turns[0].tool_steps
    assert denied.status == "denied"
    assert "write_file" in denied.denial_reason
    assert allowed.status == "ok"
    assert run.permission_denials[0]["id"] == "w1"


def test_a_spine_entry_with_no_events_at_all_is_unexecuted():
    events = _log([
        _budget(1),
        _response(turn=1, calls=[_spine("a", "read_file", path="a"),
                                 _spine("b", "read_file", path="b")]),
        _call("a", "read_file", turn=1, path="a"),
        _result("a", "read_file", turn=1),
        _metrics(1),
    ])
    run = build_run(events)
    assert [s.status for s in run.turns[0].tool_steps] == ["ok", "unexecuted"]
    assert "tool_step_unexecuted" in run.warning_codes


def test_orphan_tool_result_is_kept_not_dropped():
    events = _log([
        _budget(1),
        _response(turn=1, calls=[]),
        _result("ghost", "bash", "output nobody asked for", turn=1),
        _metrics(1),
    ])
    run = build_run(events)
    orphans = [s for s in run.turns[0].tool_steps if s.status == "orphan"]
    assert len(orphans) == 1
    assert orphans[0].content == "output nobody asked for"
    assert "orphan_tool_event" in run.warning_codes


# --- E. task_complete and the gate ------------------------------------------


def test_task_complete_step_is_synthesised_from_the_model_response():
    """It emits no tool_call/tool_result — the loop intercepts it — so the spine is
    the only place it appears."""
    run = build_run(_log(_submit_turn(1)))
    steps = run.turns[0].tool_steps
    assert len(steps) == 1
    assert steps[0].name == "task_complete"
    assert steps[0].status == "gate_approved"
    assert steps[0].gate_index == 0


def test_a_rejected_submission_is_marked_rejected():
    run = build_run(_log(_submit_turn(1, approved=False)))
    step = run.turns[0].tool_steps[0]
    assert step.status == "gate_rejected"
    assert run.turns[0].gates[0].feedback == "not enough evidence"


def test_contract_gate_reject_without_a_verification_event():
    """The contract gate can refuse while emitting only a `contract` event, so a
    submission with no verification is normal, not an anomaly."""
    events = _log([
        _budget(1),
        _response(turn=1, calls=[_spine("c1", "task_complete", summary="did it")]),
        _ev("contract", {"action": "gate_reject", "outstanding": ["c1"]}),
        _metrics(1),
    ])
    run = build_run(events)
    gate = run.turns[0].gates[0]
    assert gate.approved is False
    assert gate.contract_action == "gate_reject"
    assert gate.outstanding == ["c1"]
    assert "gate_verdict_unmatched" not in run.warning_codes


def test_a_submission_with_no_verdict_stays_unknown():
    events = _log([
        _budget(1),
        _response(turn=1, calls=[_spine("c1", "task_complete", summary="did it")]),
        _metrics(1),
    ])
    run = build_run(events)
    assert run.turns[0].tool_steps[0].status == "gate_unknown"
    assert run.turns[0].gates == []


def test_a_verdict_with_no_submission_is_flagged_not_dropped():
    events = _log([
        _budget(1),
        _response(turn=1, calls=[]),
        _ev("verification", {"approved": False, "checklist": {}, "feedback": "no",
                             "evidence": [], "attempt": 1, "turn": 1}),
        _metrics(1),
    ])
    run = build_run(events)
    assert len(run.turns[0].gates) == 1
    assert "gate_verdict_unmatched" in run.warning_codes


# --- F. turn-number pathologies ---------------------------------------------


def test_a_turn_missing_its_budget_event_still_opens():
    """The best-effort budget snapshot can be skipped silently, so a turn can
    arrive with no preflight. On a modern log `turn` still segments it."""
    events = _log(_read_turn(1), [
        _response(turn=2, calls=[_spine("b", "read_file", path="b")]),
        _call("b", "read_file", turn=2, path="b"),
        _result("b", "read_file", turn=2),
        _metrics(2),
    ])
    run = build_run(events)
    assert [t.number for t in run.turns] == [1, 2]
    assert run.turns[1].budget is None
    assert "missing_budget_snapshot" in run.warning_codes


def test_an_old_log_with_no_turn_fields_segments_positionally():
    """A pre-`turn` log: only budget and turn_metrics carry a number. This is the
    B3-plus-late-naming path, and it is what keeps the existing harbor trees
    readable."""
    events = _log(
        [
            _budget(1),
            _response(calls=[_spine("a", "read_file", path="a")]),
            _call("a", "read_file", path="a"),
            _result("a", "read_file"),
            _metrics(1),
        ],
        [
            # No budget for turn 2 either: the only signal is the second response.
            _response(calls=[_spine("b", "read_file", path="b")]),
            _call("b", "read_file", path="b"),
            _result("b", "read_file"),
            _metrics(2),
        ],
    )
    run = build_run(events)
    assert [t.number for t in run.turns] == [1, 2]
    assert [len(t.model_calls) for t in run.turns] == [1, 1]
    assert run.turns[1].tool_steps[0].call_id == "b"


def test_a_legacy_log_reports_its_format_once_not_per_turn():
    """The shape every archived Harbor trial on disk actually has.

    Garuda 1.1.0-era logs carry seven event types and no turn structure at all — no
    `budget`, no `turn_metrics`, no `turn` field. They still segment one band per
    model response, but "no number / no budget / never finished" are then facts about
    the *format*. Reported per turn they badge every turn of every archived run as
    hung, which is how a real check of 138 trials and 5,980 turns found this.
    """
    events = [
        _start(),
        _ev("environment_snapshot", {"chars": 512}),
        _ev("user_message", {"content": "do it"}),
        _response(calls=[_spine("a", "read_file", path="a")]),
        _call("a", "read_file", path="a"),
        _result("a", "read_file"),
        _response(calls=[_spine("b", "read_file", path="b")]),
        _call("b", "read_file", path="b"),
        _result("b", "read_file"),
        _ev("session_end", {"success": True, "turns": 2}),
    ]
    run = build_run(events)
    assert run.turn_count == 2, "one band per model response"
    assert [t.tool_steps[0].call_id for t in run.turns] == ["a", "b"]
    assert "legacy_log_format" in run.warning_codes
    for code in ("turn_never_finished", "missing_budget_snapshot", "turn_number_unknown"):
        assert code not in run.warning_codes, f"{code} is a format property here, not a finding"
    assert run.success is True
    assert _coverage(run) == list(range(len(events)))


def test_steering_budget_events_alone_are_not_turn_structure():
    """A real middle vintage on disk: `budget` events exist, but only for the
    *steering* stages (the floats 0.5/0.8 and `final_turn`), never the per-turn
    `stage="context"` preflight.

    Those three payloads do carry a `turn`, so a naive "does any event mention a
    turn" test reads them as turn structure and re-enables the per-turn warnings for
    every band in the run — 3 events out of several hundred, on 79 archived trials.
    The gate is boundary anchors specifically.

    They must also not *split* a band: the open band is unnumbered, so a numbered
    steering event has to be absorbed rather than starting a new turn.
    """
    events = [
        _start(),
        _ev("user_message", {"content": "do it"}),
        _response(calls=[_spine("a", "bash", command="ls")]),
        _call("a", "bash", command="ls"),
        _result("a", "bash"),
        _ev("budget", {"stage": 0.5, "turn": 3, "elapsed_sec": 12.0}),
        _response(calls=[_spine("b", "bash", command="pwd")]),
        _call("b", "bash", command="pwd"),
        _result("b", "bash"),
        _ev("budget", {"stage": "final_turn", "turn": 4}),
        _ev("session_end", {"success": False, "reason": "max_turns", "turns": 2}),
    ]
    run = build_run(events)
    assert run.turn_count == 2, "the steering events must not split a band"
    assert "legacy_log_format" in run.warning_codes
    for code in ("turn_never_finished", "missing_budget_snapshot", "turn_number_unknown"):
        assert code not in run.warning_codes, code
    # The payloads are still kept verbatim on the band that contained them, floats
    # and all, so nothing is lost by declining to treat them as boundaries.
    assert run.turns[0].budget_reviews[0]["stage"] == 0.5
    assert _coverage(run) == list(range(len(events)))


def test_a_modern_log_still_flags_a_genuinely_unfinished_turn():
    """The other side of the same rule: one turn-bearing event is enough to make the
    per-turn warnings meaningful again, which is the live/killed-run case."""
    events = _log([_budget(1), _response(turn=1, calls=[])], end=False)
    run = build_run(events)
    assert "legacy_log_format" not in run.warning_codes
    assert "turn_never_finished" in run.warning_codes


def test_unfinished_turn_is_flagged_never_finished():
    events = _log([_budget(1), _response(turn=1, calls=[])], end=False)
    run = build_run(events)
    assert run.turns[-1].finished is False
    assert "turn_never_finished" in run.warning_codes
    message = next(a.message for a in run.warnings if a.code == "turn_never_finished")
    assert "TURN NEVER FINISHED" in message


def test_final_submission_is_a_separate_turn_sharing_a_number():
    """The labelled band reuses the previous turn's number, so it cannot be
    segmented by number — the budget marker is what opens it."""
    events = _log(
        _read_turn(1), _read_turn(2, call_id="b"), _read_turn(3, call_id="c"),
        [
            _ev("budget", {"stage": "final_submission", "turn": 3, "attempted": True}),
            _response(turn=3, calls=[_spine("f1", "task_complete", summary="best effort")]),
            _ev("verification", {"approved": True, "checklist": {}, "feedback": None,
                                 "evidence": [], "attempt": 1, "turn": 3}),
            _metrics(3, label="final_submission"),
        ],
    )
    run = build_run(events)
    assert len(run.turns) == 4
    assert [t.number for t in run.turns] == [1, 2, 3, 3]
    assert run.turns[3].label == "final_submission"
    assert run.turns[3].is_extra is True
    # Matches RunMetrics.summary()["turns"], which counts working turns only.
    assert run.turn_count == 3
    assert "duplicate_turn_number" not in run.warning_codes


def test_a_labelled_metrics_record_opens_its_band_late():
    """If the budget marker is missing (a truncated or older log), the labelled
    turn_metrics must not overwrite the working turn's metrics."""
    events = _log(_read_turn(1), [
        _response(turn=1, calls=[_spine("f1", "task_complete", summary="best effort")]),
        _metrics(1, label="final_submission"),
    ])
    run = build_run(events)
    assert len(run.turns) == 2
    assert run.turns[0].label is None
    assert run.turns[0].metrics is not None
    assert run.turns[1].label == "final_submission"


def test_duplicate_unlabelled_turn_number_warns():
    events = _log(_read_turn(1), _read_turn(2, call_id="b"))
    events.extend([_budget(2), _response(turn=2, calls=[]), _metrics(2)])
    run = build_run(events)
    assert "duplicate_turn_number" in run.warning_codes


def test_a_turn_number_gap_warns():
    run = build_run(_log(_read_turn(1), _read_turn(2, call_id="b"), _read_turn(4, call_id="d")))
    assert [t.number for t in run.turns] == [1, 2, 4], "order preserved, nothing invented"
    assert "turn_number_gap" in run.warning_codes
    assert "3" in next(a.message for a in run.warnings if a.code == "turn_number_gap")


def test_turn_zero_is_accepted():
    """max_turns=0 leaves the counter at 0 and still emits a final-submission band."""
    events = _log([
        _ev("budget", {"stage": "final_submission", "turn": 0, "attempted": True}),
        _response(turn=0, calls=[]),
        _metrics(0, label="final_submission"),
    ])
    run = build_run(events)
    assert run.turns[0].number == 0
    assert "turn_number_unknown" not in run.warning_codes


# --- G. compaction ----------------------------------------------------------


def test_a_proactive_compaction_reports_what_it_did():
    events = _log(_read_turn(1), [
        _ev("summarization", {"turn": 2, "duration_ms": 12.0, "strategy": "microcompact",
                              "action": "prune", "pruned": 4, "messages_before": 20,
                              "messages_after": 20, "tokens_before": 900, "tokens_after": 300}),
        *_read_turn(2, call_id="b"),
    ])
    compaction = build_run(events).turns[1].compactions[0]
    assert compaction.reason is None
    assert compaction.action == "prune"
    assert compaction.pruned == 4
    assert compaction.tokens_before == 900
    assert compaction.tokens_after == 300


def test_a_context_overflow_yields_one_model_call_not_two():
    """`_complete_or_shrink` catches the overflow inside the model call, so the
    marker precedes a single response rather than sitting between two."""
    events = _log([
        _budget(3),
        _ev("summarization", {"turn": 3, "reason": "context_overflow", "recovered": True,
                              "duration_ms": 40.0, "gauge_said": 9, "capacity": 8}),
        _response(turn=3, calls=[]),
        _metrics(3),
    ])
    run = build_run(events)
    turn = run.turns[0]
    assert len(turn.model_calls) == 1
    assert "multiple_model_calls" not in run.warning_codes
    assert turn.compactions[0].reason == "context_overflow"
    assert turn.compactions[0].recovered is True


# --- H. termination shapes --------------------------------------------------


def test_turn_metrics_after_session_end_still_closes_its_turn():
    """RunState.result() flushes metrics after the loop emitted session_end, so
    session_end can neither close the band nor end the walk."""
    events = [
        _start(),
        _ev("user_message", {"content": "do it"}),
        _budget(1),
        _response(turn=1, calls=[]),
        _ev("session_end", {"success": True, "turns": 1}),
        _ev("turn_metrics", {"turn": 1, "model_ms": 5.0}, at=30),
    ]
    run = build_run(events)
    assert run.turns[-1].finished is True
    assert run.success is True
    assert run.ended_at == _ts(30), "ended_at is the last event, not session_end"
    assert 4 in run.trailing_events
    assert "turn_never_finished" not in run.warning_codes


def test_session_end_without_turns_reports_none():
    events = _log([_budget(1), _response(turn=1, calls=[]), _metrics(1)],
                  end=_ev("session_end", {"success": False, "reason": "model_error",
                                          "error": "RuntimeError: boom"}))
    run = build_run(events)
    assert run.reported_turns is None
    assert run.end_reason == "model_error"
    assert run.success is False
    assert run.turn_count == 1


def test_session_end_via_final_submission_is_surfaced():
    events = _log(_read_turn(1),
                  end=_ev("session_end", {"success": True, "turns": 5,
                                          "via": "final_submission"}))
    run = build_run(events)
    assert run.end_via == "final_submission"
    assert run.reported_turns == 5


def test_a_missing_session_end_leaves_success_unknown():
    """An unknown outcome must never render as a failure."""
    run = build_run(_log(_read_turn(1), end=False))
    assert run.complete is False
    assert run.success is None
    assert "no_session_end" in run.warning_codes


def test_environment_unavailable_attaches_to_its_turn():
    events = _log(_read_turn(1), [
        _budget(2),
        _ev("environment_unavailable", {"turn": 2, "reason": "container_gone",
                                        "detail": "docker exec failed"}),
    ], end=_ev("session_end", {"success": False, "reason": "environment_unavailable",
                               "detail": "docker exec failed"}))
    run = build_run(events)
    assert run.turns[1].environment_unavailable["reason"] == "container_gone"
    assert run.end_reason == "environment_unavailable"
    assert run.reported_turns is None


# --- I. rigorous phases -----------------------------------------------------


def test_a_plain_run_has_one_main_phase():
    run = build_run(_log(_read_turn(1), _read_turn(2, call_id="b")))
    assert len(run.phases) == 1
    assert run.phases[0].kind == "main"
    assert all(turn.phase_index == 0 for turn in run.turns)


def test_the_plan_marker_closes_the_plan_phase():
    """`[rigorous:plan]` fires after the plan sub-run returned, so it terminates
    that phase rather than opening it — and it explains the turn-number reset that
    follows, which must therefore not warn."""
    events = _log(
        _read_turn(1), _read_turn(2, call_id="b"),
        [_ev("user_message", {"content": "[rigorous:plan] write the file then verify"})],
        _read_turn(1, call_id="e1"), _read_turn(2, call_id="e2"),
        [_ev("verification", {"approved": True, "feedback": "looks good", "phase": "critic",
                              "attempt": 0})],
        start=_start(mode="rigorous"),
    )
    run = build_run(events)
    assert run.mode == "rigorous"
    assert [phase.kind for phase in run.phases] == ["plan", "execute"]
    assert run.phases[0].summary == "write the file then verify"
    assert run.phases[0].turn_end == 2
    assert run.phases[1].critic.approved is True
    assert run.phases[1].critic.kind == "critic"
    assert "turn_number_regressed" not in run.warning_codes


def test_the_critic_verdict_is_phase_level_not_a_turn_gate():
    events = _log(
        _read_turn(1),
        [_ev("verification", {"approved": False, "feedback": "no tests", "phase": "critic",
                              "attempt": 0})],
        start=_start(mode="rigorous"),
    )
    run = build_run(events)
    assert all(turn.gates == [] for turn in run.turns), "the critic is not a turn gate"
    critic = next(p.critic for p in run.phases if p.critic)
    assert critic.checklist == {}
    assert critic.evidence == []


def test_a_turn_reset_without_a_marker_warns():
    events = _log(_read_turn(1), _read_turn(2, call_id="b"),
                  _read_turn(1, call_id="c"), _read_turn(2, call_id="d"))
    run = build_run(events)
    assert "turn_number_regressed" in run.warning_codes
    assert any(phase.kind == "unknown" for phase in run.phases)
    assert _coverage(run) == list(range(len(events)))


# --- J. subagents -----------------------------------------------------------


def test_a_subagent_user_message_is_not_a_phase_boundary():
    """It is appended to the parent store from inside invoke_subagent, i.e. between
    that tool's call and its result."""
    events = _log([
        _budget(1),
        _response(turn=1, calls=[_spine("s1", "invoke_subagent", profile="explore")]),
        _call("s1", "invoke_subagent", turn=1, profile="explore"),
        _ev("user_message", {"content": "[subagent:explore] found it",
                             "subagent": "explore", "success": True}),
        _result("s1", "invoke_subagent", "found it", turn=1),
        _metrics(1),
    ])
    run = build_run(events)
    assert len(run.phases) == 1
    assert run.turns[0].tool_steps[0].status == "ok"
    assert run.turns[0].tool_steps[0].content == "found it"
    assert run.subagent_calls[0]["subagent"] == "explore"


# --- K. usage, cost, and the config payload ---------------------------------


def test_cost_is_summed_per_call_not_from_merged_usage():
    """costs.merge_usage drops cost_usd, so merging first would discard every
    provider-reported invoice."""
    events = _log(
        [_budget(1), _response(turn=1, calls=[],
                               usage={"prompt_tokens": 100, "completion_tokens": 50,
                                      "cost_usd": 0.001}), _metrics(1)],
        [_budget(2), _response(turn=2, calls=[],
                               usage={"prompt_tokens": 200, "completion_tokens": 60,
                                      "cost_usd": 0.002}), _metrics(2)],
    )
    run = build_run(events)
    assert run.cost_usd == 0.003
    assert run.usage["prompt_tokens"] == 300
    assert run.usage["completion_tokens"] == 110


def test_cost_is_unavailable_when_any_call_cannot_be_priced():
    events = _log(
        [_budget(1), _response(turn=1, calls=[],
                               usage={"prompt_tokens": 100, "completion_tokens": 50,
                                      "cost_usd": 0.001}), _metrics(1)],
        [_budget(2), _response(turn=2, calls=[],
                               usage={"prompt_tokens": 200, "completion_tokens": 60}),
         _metrics(2)],
        start=_start(model="who/knows-what-this-costs"),
    )
    run = build_run(events)
    assert run.cost_usd is None, "understating spend silently is the failure to avoid"
    assert "cost_unavailable" in run.warning_codes
    assert run.turns[0].model_calls[0].cost_usd == 0.001


def test_metrics_are_kept_verbatim_and_key_numbers_coerced():
    """Forward compatible: an unknown field survives, and a stringified number from
    an older disk still totals."""
    events = _log([
        _budget(1),
        _response(turn=1, calls=[]),
        _ev("turn_metrics", {"turn": 1, "model_ms": "1234", "future_field": 1,
                             "tool_errors": 2}),
    ])
    turn = build_run(events).turns[0]
    assert turn.metrics["future_field"] == 1
    assert turn.model_ms == 1234.0
    assert turn.tool_errors == 2


def test_the_resolved_gate_stack_is_exposed():
    from garuda.core.modes import GATE_FIELDS

    config = dict.fromkeys(GATE_FIELDS, True)
    config.update({"enable_verifier": True, "max_turns": 40, "permission_mode": "yolo"})
    run = build_run(_log(_read_turn(1), start=_start(mode="eval", config=config)))
    assert run.config["max_turns"] == 40
    assert run.gate_stack == dict.fromkeys(GATE_FIELDS, True)
    assert run.permission_mode is None  # not in this fixture's session_start


def test_an_old_log_has_no_gate_stack_at_all():
    """Absence stays ambiguous rather than being reported as 'all gates off'."""
    run = build_run(_log(_read_turn(1)))
    assert run.config is None
    assert run.gate_stack is None


def test_non_integer_turn_values_are_handled():
    events = _log([
        _ev("budget", {"stage": "context", "turn": "2"}),
        _response(turn=None, calls=[]),
        _ev("turn_metrics", {"turn": True}),
    ])
    run = build_run(events)
    # "2" coerces; True is ignored rather than read as turn 1.
    assert run.turns[0].number == 2


def test_to_dict_is_json_serialisable_and_carries_the_aggregates():
    run = build_run(_log(_read_turn(1), _submit_turn(2)))
    payload = run.to_dict()
    json.dumps(payload)  # must not raise
    assert payload["turn_count"] == 2
    assert payload["usage"]["prompt_tokens"] == 20
    assert payload["warning_codes"] == sorted(run.warning_codes)
    assert payload["turns"][0]["is_extra"] is False
    assert "events" in payload
    assert "events" not in run.to_dict(include_events=False)


# --- L. the incremental tail ------------------------------------------------


def _write(path, events, *, partial=None):
    body = "".join(json.dumps(e) + "\n" for e in events)
    if partial:
        body += partial
    path.write_text(body)


def test_the_tail_advances_only_past_complete_lines(tmp_path):
    path = tmp_path / "events.jsonl"
    first, second = _start(), _ev("user_message", {"content": "do it"})
    _write(path, [first], partial=json.dumps(second)[:20])

    tail = EventTail(path)
    assert len(tail.read_new()) == 1
    held = tail.offset
    assert any(w.code == "torn_tail" for w in tail.warnings)

    _write(path, [first, second])
    delivered = tail.read_new()
    assert len(delivered) == 1, "the completed event arrives exactly once"
    assert delivered[0]["type"] == "user_message"
    assert tail.offset > held


def test_the_tail_returns_nothing_when_the_file_is_unchanged(tmp_path):
    path = tmp_path / "events.jsonl"
    _write(path, [_start()])
    tail = EventTail(path)
    tail.read_new()
    at = tail.offset
    assert tail.read_new() == []
    assert tail.offset == at


def test_the_tail_detects_truncation_and_rereads(tmp_path):
    """events.save() overwrites, so a re-run Harbor trial dir shrinks its log."""
    path = tmp_path / "events.jsonl"
    _write(path, [_start(), _ev("user_message", {"content": "long task"})])
    tail = EventTail(path)
    assert len(tail.read_new()) == 2

    _write(path, [_start()])
    again = tail.read_new()
    assert tail.restarts == 1
    assert len(again) == 1
    assert any(w.code == "file_truncated" for w in tail.warnings)


def test_the_tail_survives_a_multibyte_payload_split_across_reads(tmp_path):
    """The byte-cursor guarantee, on a log that really does contain multibyte UTF-8.

    `EventStore` writes with `json.dumps`, whose default `ensure_ascii=True` escapes
    non-ASCII to `\\uXXXX` — so Garuda's own logs are pure ASCII and this can only
    arise from a hand-edited or externally-written file. Worth pinning anyway,
    because it is the property that makes a byte offset safe at all: cutting on
    `b"\\n"` cannot split a character, since 0x0A never appears inside a multi-byte
    UTF-8 sequence.
    """
    path = tmp_path / "events.jsonl"
    rocket = "\U0001f680"
    emoji = _ev("user_message", {"content": f"look: {rocket} done"})
    raw = (json.dumps(emoji, ensure_ascii=False) + "\n").encode("utf-8")
    assert rocket.encode("utf-8") in raw, "the fixture must be genuinely multibyte"

    # Cut two bytes into the 4-byte sequence.
    cut = raw.index(rocket.encode("utf-8")) + 2
    path.write_bytes(raw[:cut])
    tail = EventTail(path)
    assert tail.read_new() == [], "an incomplete line is held back, never decoded"

    path.write_bytes(raw)
    delivered = tail.read_new()
    assert delivered[0]["payload"]["content"] == f"look: {rocket} done"
    assert tail.offset == len(raw)


def test_a_missing_file_is_not_an_error(tmp_path):
    tail = EventTail(tmp_path / "not-yet.jsonl")
    assert tail.read_new() == []
    assert tail.offset == 0


# --- M. the live reader -----------------------------------------------------


def test_refresh_returns_the_same_object_when_nothing_was_appended(tmp_path):
    path = tmp_path / "events.jsonl"
    _write(path, _log(_read_turn(1)))
    reader = TrajectoryReader(path)
    first = reader.refresh()
    assert reader.refresh() is first, "a free change check for the caller"


def test_refresh_picks_up_appended_turns(tmp_path):
    path = tmp_path / "events.jsonl"
    prefix = [_start(), _ev("user_message", {"content": "do it"}), *_read_turn(1)]
    _write(path, prefix)
    reader = TrajectoryReader(path)
    run = reader.refresh()
    assert run.turn_count == 1
    first_turn = run.turns[0]

    _write(path, [*prefix, *_read_turn(2, call_id="b")])
    grown = reader.refresh()
    assert grown is not run
    assert grown.turn_count == 2
    assert reader.event_count == len(prefix) + 5
    # Deterministic re-derivation: turn 1 comes back identical, so a UI diffing
    # against the previous render sees no spurious churn.
    assert grown.turns[0] == first_turn


def test_an_open_turn_becomes_finished_on_refresh(tmp_path):
    """The live-dashboard case: mid-turn, then complete."""
    path = tmp_path / "events.jsonl"
    prefix = [_start(), _ev("user_message", {"content": "do it"}), _budget(1),
              _response(turn=1, calls=[])]
    _write(path, prefix)
    reader = TrajectoryReader(path)
    run = reader.refresh()
    assert run.turns[-1].finished is False
    assert "turn_never_finished" in run.warning_codes

    _write(path, [*prefix, _metrics(1), _ev("session_end", {"success": True, "turns": 1})])
    done = reader.refresh()
    assert done.turns[-1].finished is True
    assert done.complete is True
    assert "turn_never_finished" not in done.warning_codes


def test_the_reader_replaces_its_events_after_a_truncation(tmp_path):
    """Extending instead of replacing would double every surviving event."""
    path = tmp_path / "events.jsonl"
    _write(path, _log(_read_turn(1), _read_turn(2, call_id="b")))
    reader = TrajectoryReader(path)
    assert reader.refresh().turn_count == 2

    _write(path, _log(_read_turn(1)))
    rebuilt = reader.refresh()
    assert rebuilt.turn_count == 1
    assert reader.event_count == 8
    # And a further append still behaves normally afterwards.
    _write(path, _log(_read_turn(1), _read_turn(2, call_id="b")))
    assert reader.refresh().turn_count == 2


def test_the_reader_accepts_a_harbor_trial_directory(tmp_path):
    trial = tmp_path / "task__abc1234"
    (trial / "agent").mkdir(parents=True)
    _write(trial / "agent" / "events.jsonl", _log(_read_turn(1)))
    run = TrajectoryReader(trial).refresh()
    assert run.turn_count == 1
    assert run.source.endswith("agent/events.jsonl")
