"""#856: `flow show` (and `--json` `live`) says what a running or paused step is doing.

The live status is one read-only function, ``read_model.flow_in_flight``,
surfaced as the read model's ``flow()["in_flight"]``: it reads the journal's
open intent, the attempt's launch record and progress file and the step
session's meta, and never takes a lease.
"""

from __future__ import annotations

import json
import uuid

from garuda.core import read_model
from garuda.core.sessions import SessionStore
from garuda.flows import engine, launches
from garuda.runtime.ownership import current_owner
from garuda.runtime.session_state import finished, started
from tests.test_flows import repo as _repo
from tests.test_runtime_cli import _main

repo = _repo  # the fixture


def _flow(repo, *, state="running", stop=None):
    store = SessionStore()
    flow_id = str(uuid.uuid4())
    store.begin(flow_id, task="t", model="flow", agent="flow:pb", workspace=str(repo))
    store.update_meta(flow_id, {
        "kind": "flow", "flow": {"name": "pb", "steps": ["build"]}, "flow_state": state,
        "status": "running" if state == "running" else "failed",
        "state": started(current_owner().to_dict()) if state == "running" else finished(success=False),
        **({"flow_stop": stop} if stop else {})})
    directory = engine.flow_dir(store, flow_id)
    (directory / "receipts").mkdir(parents=True)
    return store, flow_id, directory


def _running_step(store, flow_id, directory, repo):
    step_id = str(uuid.uuid4())
    store.begin(step_id, task="t", model="acp:fake", agent="fake", workspace=str(repo))
    store.update_meta(step_id, {"limits": {"deadline_sec": 3600},
                                "flow_step": {"flow_session": flow_id, "step": "build",
                                              "attempt": 2}})
    with open(directory / engine.JOURNAL, "a") as journal:
        journal.write(json.dumps({"event": "intent", "index": 0, "step": "build", "attempt": 2,
                                  "at": "2026-11-27T10:00:00+00:00"}) + "\n")
    launches.write(directory, "build", 2, {"session_id": step_id,
                                           "started_at": "2026-11-27T10:00:00+00:00"})
    progress = directory / "progress"
    progress.mkdir()
    rows = [{"event": "start", "at": "2026-11-27T10:00:01+00:00", "attempt": 2,
             "session_id": step_id},
            {"turn": 1, "at": "2026-11-27T10:05:00+00:00", "session": 1, "decision": "next",
             "stop_reason": "end_turn", "context": {"used": 90000, "size": 200000},
             "note": "wrote the parser"},
            {"turn": 2, "at": "2026-11-27T10:09:00+00:00", "session": 1, "decision": "rotate",
             "stop_reason": "end_turn", "context": {"used": 170000, "size": 200000}},
            {"turn": 3, "at": "2026-11-27T10:12:00+00:00", "session": 2, "decision": "next",
             "stop_reason": "max_tokens", "context": None}]
    (progress / "build-2.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    return step_id


def test_a_running_long_step_shows_its_live_status(repo, monkeypatch, capsys):
    store, flow_id, directory = _flow(repo)
    step_id = _running_step(store, flow_id, directory, repo)

    live = read_model.flow_in_flight(store, flow_id)
    assert {key: live[key] for key in (
        "state", "step", "attempt", "session_id", "turns", "harness_sessions", "rotations",
        "continuations", "last_turn_at", "last_decision", "last_stop_reason", "context_used",
        "context_size", "progress", "started_at", "deadline_at", "paused")} == {
        "state": "running", "step": "build", "attempt": 2, "session_id": step_id, "turns": 3,
        "harness_sessions": 2, "rotations": 1, "continuations": 2,
        "last_turn_at": "2026-11-27T10:12:00+00:00", "last_decision": "next",
        "last_stop_reason": "max_tokens", "context_used": 170000, "context_size": 200000,
        "progress": "progress/build-2.jsonl", "started_at": "2026-11-27T10:00:00+00:00",
        "deadline_at": "2026-11-27T11:00:00+00:00", "paused": None}
    assert live["process"] == "live" and isinstance(live["idle_sec"], int)
    assert live["running"] is True
    assert live["last_activity_at"]
    # The shared read model carries the same view.
    view = read_model.flow(store, flow_id)["in_flight"]
    assert (view["session_id"], view["turns"], view["rotations"]) == (step_id, 3, 1)

    code, out = _main(monkeypatch, capsys, "flow", "show", flow_id, "--workspace", str(repo))
    assert code == 0, out
    assert ("live: step build (attempt 2), turn 3 in 2 harness session(s), 1 rotation(s), "
            "2 continuation(s)") in out
    assert f"session {step_id}; started 2026-11-27T10:00:00+00:00; last activity" in out
    assert "last turn: next (stop reason max_tokens); context 170000/200000 (85%)" in out
    assert "deadline 2026-11-27T11:00:00+00:00" in out

    code, out = _main(monkeypatch, capsys, "flow", "show", flow_id, "--workspace", str(repo),
                      "--json")
    assert code == 0, out
    shown = json.loads(out)
    assert shown["live"]["session_id"] == step_id and shown["live"]["turns"] == 3
    # Read only: nothing was written and no lease was taken.
    from garuda.workspace.lease import LeaseStore

    assert LeaseStore().live_holders_for_session(flow_id) == []
    assert engine.journal(store, flow_id)[-1]["event"] == "intent"


def test_a_step_before_its_first_turn_ends_is_visible(repo):
    store, flow_id, directory = _flow(repo)
    with open(directory / engine.JOURNAL, "a") as journal:
        journal.write(json.dumps({"event": "intent", "index": 0, "step": "build", "attempt": 1,
                                  "at": "2026-11-27T10:00:00+00:00"}) + "\n")
    session = str(uuid.uuid4())
    (directory / "progress").mkdir()
    (directory / "progress" / "build-1.jsonl").write_text(json.dumps(
        {"event": "start", "at": "2026-11-27T10:00:01+00:00", "attempt": 1,
         "session_id": session}) + "\n")
    live = read_model.flow_in_flight(store, flow_id)
    assert (live["step"], live["attempt"], live["session_id"], live["turns"]) == (
        "build", 1, session, None)


def test_a_paused_step_shows_its_stop(repo, monkeypatch, capsys):
    stop = {"code": "flow.step_no_progress", "step": "build",
            "message": "flow.step_no_progress: step build made no progress for 3 turns"}
    store, flow_id, directory = _flow(repo, state="stopped", stop=stop)
    with open(directory / engine.JOURNAL, "a") as journal:
        for event in ("intent", "receipt"):
            journal.write(json.dumps({"event": event, "index": 0, "step": "build",
                                      "attempt": 1}) + "\n")
    (directory / "receipts" / "build-1.json").write_text(json.dumps(
        {"index": 0, "step": "build", "attempt": 1, "status": "stopped", "inputs": [],
         "outputs": [], "stop": stop}))
    live = read_model.flow_in_flight(store, flow_id)
    assert live["paused"]["code"] == "flow.step_no_progress"
    assert (live["step"], live["attempt"], live["started_at"]) == ("build", 1, None)
    code, out = _main(monkeypatch, capsys, "flow", "show", flow_id, "--workspace", str(repo))
    assert code == 0, out
    assert "stopped at step build: flow.step_no_progress" in out
    assert "paused after step build (attempt 1)" in out
    assert out.count("flow.step_no_progress:") == 1  # the code is printed once


def test_a_finished_flow_has_no_live_status(repo):
    store, flow_id, _directory = _flow(repo, state="completed")
    assert read_model.flow_in_flight(store, flow_id) is None
    assert read_model.flow(store, flow_id)["in_flight"] is None


def test_a_stopped_flow_prints_its_last_attempt_not_a_live_line(repo, monkeypatch, capsys):
    """A flow stopped before any turn (e.g. role.option_not_applied) runs nothing: `flow
    show` says so, as `--json` (`process: exited`, `running: false`) does."""
    stop = {"code": "role.option_not_applied", "step": "build",
            "message": "role.option_not_applied: the agent reports mode='acceptEdits'"}
    store, flow_id, directory = _flow(repo, state="stopped", stop=stop)
    step_id = str(uuid.uuid4())
    with open(directory / engine.JOURNAL, "a") as journal:
        for event in ("intent", "receipt"):
            journal.write(json.dumps({"event": event, "index": 0, "step": "build", "attempt": 1,
                                      "at": "2026-11-27T12:49:02+00:00"}) + "\n")
    launches.write(directory, "build", 1, {"session_id": step_id,
                                           "started_at": "2026-11-27T12:49:03+00:00"})
    (directory / "receipts" / "build-1.json").write_text(json.dumps(
        {"index": 0, "step": "build", "attempt": 1, "status": "stopped", "inputs": [],
         "outputs": [], "session_id": step_id, "stop": stop}))

    code, out = _main(monkeypatch, capsys, "flow", "show", flow_id, "--workspace", str(repo))
    assert code == 0, out
    assert "live:" not in out and "no turn finished yet" not in out
    assert ("last attempt: step build (attempt 1), stopped (process exited), no turn finished"
            in out)
    assert "stopped at step build: role.option_not_applied" in out
    assert "paused after step build (attempt 1)" in out
    live = read_model.flow_in_flight(store, flow_id)
    assert (live["state"], live["process"], live["running"]) == ("stopped", "exited", False)
    assert live["session_id"] == step_id and live["paused"]["code"] == "role.option_not_applied"
    code, out = _main(monkeypatch, capsys, "flow", "show", flow_id, "--workspace", str(repo),
                      "--json")
    assert code == 0, out
    assert json.loads(out)["live"]["running"] is False


def test_a_checked_usage_limit_stop_prints_a_pause_with_its_reset(repo, monkeypatch, capsys):
    """`flow show` prints a checked flow's usage-limit stop (`flow.step_failed`, cause
    `harness.limit_reached`) as a pause until the reset and what to run then."""
    from garuda.acp.protocol import reset_time_text

    stop = {"code": "flow.step_failed", "step": "build", "message": "usage limit",
            "cause": {"code": "harness.limit_reached",
                      "limit": {"resets_at": 1_900_000_000, "rate_limit_type": "five_hour"}}}
    _store, flow_id, _directory = _flow(repo, state="stopped", stop=stop)
    code, out = _main(monkeypatch, capsys, "flow", "show", flow_id, "--workspace", str(repo))
    assert code == 0, out
    assert (f"paused on a usage limit until {reset_time_text(1_900_000_000)}; run "
            f"`garuda flow resume {flow_id}` after 2030-03-17T17:46:40+00:00") in out
    plain = {"code": "flow.step_failed", "step": "build", "message": "it failed"}
    _store, flow_id, _directory = _flow(repo, state="stopped", stop=plain)
    code, out = _main(monkeypatch, capsys, "flow", "show", flow_id, "--workspace", str(repo))
    assert code == 0, out
    assert "paused on a usage limit" not in out
