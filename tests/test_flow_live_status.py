"""#856: `flow show` (and `--json` `live`) says what a running or paused step is doing.

The live status is one read-only function, ``read_model.flow_in_flight``,
surfaced as the read model's ``flow()["in_flight"]``: it reads the journal's
open intent, the attempt's launch record and progress file and the step
session's meta, and never takes a lease.
"""

from __future__ import annotations

import json
import uuid

import pytest

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


# --- #856 follow-up: silence judgement and recent turns --------------------------------

def _limit_step(store, flow_id, directory, repo, limits):
    step_id = _running_step(store, flow_id, directory, repo)
    store.update_meta(step_id, {"limits": limits})
    return step_id


def test_silence_limits_are_the_step_sessions_effective_limits(repo):
    store, flow_id, directory = _flow(repo)
    _limit_step(store, flow_id, directory, repo,
                {"deadline_sec": 3600, "silence_sec": 600.0, "tool_silence_sec": 7200.0})
    live = read_model.flow_in_flight(store, flow_id)
    assert (live["silence_limit_sec"], live["tool_silence_limit_sec"]) == (600.0, 7200.0)
    # No status file yet: the tool and approval facts are unknown, the state is judged.
    assert (live["tool_in_flight"], live["approval_parked"]) == (None, None)
    assert live["silence_state"] in ("ok", "warn", "over")


def test_without_silence_limits_they_are_null_and_the_state_is_ok(repo, monkeypatch, capsys):
    store, flow_id, directory = _flow(repo)
    _running_step(store, flow_id, directory, repo)  # limits: deadline only
    live = read_model.flow_in_flight(store, flow_id)
    assert (live["silence_limit_sec"], live["tool_silence_limit_sec"]) == (None, None)
    assert live["silence_state"] == "ok"
    code, out = _main(monkeypatch, capsys, "flow", "show", flow_id, "--workspace", str(repo))
    assert code == 0, out
    assert out.count("    silence: ") == 1
    assert "    silence: ok (no silence limit)" in out
    code, out = _main(monkeypatch, capsys, "flow", "show", flow_id, "--workspace", str(repo),
                      "--json")
    shown = json.loads(out)["live"]
    for key in ("silence_limit_sec", "tool_silence_limit_sec", "tool_in_flight",
                "approval_parked", "silence_state", "recent_turns"):
        assert key in shown


@pytest.mark.parametrize("fields, state", [
    ({"idle_sec": 10, "silence_limit_sec": 600}, "ok"),
    ({"idle_sec": 10, "silence_limit_sec": None}, "ok"),
    ({"idle_sec": 480, "silence_limit_sec": 600}, "warn"),
    ({"idle_sec": 600, "silence_limit_sec": 600}, "over"),
    ({"idle_sec": 900, "silence_limit_sec": 600, "tool_in_flight": True,
      "tool_silence_limit_sec": 7200}, "tool_running"),
    ({"idle_sec": 900, "tool_in_flight": True}, "tool_running"),
    ({"idle_sec": 6000, "silence_limit_sec": 600, "tool_in_flight": True,
      "tool_silence_limit_sec": 7200}, "warn"),
    ({"idle_sec": 7200, "tool_in_flight": True, "tool_silence_limit_sec": 7200}, "over"),
    ({"idle_sec": 9000, "silence_limit_sec": 600, "tool_in_flight": True,
      "approval_parked": True, "tool_silence_limit_sec": 7200}, "approval_parked"),
    ({"idle_sec": None, "silence_limit_sec": 600}, "ok"),
])
def test_each_silence_state(fields, state):
    assert read_model.silence_state(fields) == state


@pytest.mark.parametrize("record, state, line", [
    ({"tool_in_flight": True, "approval_parked": False}, "tool_running",
     "silence: tool_running (idle"),
    ({"tool_in_flight": False, "approval_parked": True}, "approval_parked",
     "silence: approval_parked (a parked approval never counts as silence)"),
])
def test_the_status_file_shapes_the_live_view(repo, monkeypatch, capsys, record, state, line):
    from garuda.flows import live_status

    store, flow_id, directory = _flow(repo)
    _limit_step(store, flow_id, directory, repo,
                {"deadline_sec": 3600, "silence_sec": 600.0, "tool_silence_sec": 7200.0})
    live_status.path(directory, "build", 2).write_text(json.dumps(
        {**record, "updated_at": "2026-11-27T10:13:00+00:00"}))
    live = read_model.flow_in_flight(store, flow_id)
    assert (live["tool_in_flight"], live["approval_parked"]) == (
        record["tool_in_flight"], record["approval_parked"])
    assert live["silence_state"] == state
    code, out = _main(monkeypatch, capsys, "flow", "show", flow_id, "--workspace", str(repo))
    assert code == 0, out
    assert line in out and out.count("    silence: ") == 1


def test_a_paused_flow_has_no_silence_state(repo):
    stop = {"code": "flow.step_silent", "step": "build", "message": "silent"}
    store, flow_id, directory = _flow(repo, state="stopped", stop=stop)
    with open(directory / engine.JOURNAL, "a") as journal:
        for event in ("intent", "receipt"):
            journal.write(json.dumps({"event": event, "index": 0, "step": "build",
                                      "attempt": 1}) + "\n")
    live = read_model.flow_in_flight(store, flow_id)
    assert live["paused"]["code"] == "flow.step_silent"
    assert (live["silence_state"], live["tool_in_flight"]) == (None, None)


def test_recent_turns_are_bounded_and_newest_last(repo):
    store, flow_id, directory = _flow(repo)
    _running_step(store, flow_id, directory, repo)
    rows = [{"turn": n, "at": f"2026-11-27T11:{n:02d}:00+00:00", "session": 1 + n // 10,
             "decision": "next", "stop_reason": "end_turn", "elapsed_sec": 60.0 * n,
             "changed_total": n, "context": {"used": 1000 * n, "size": 200000}}
            for n in range(1, 26)]
    with open(directory / "progress" / "build-2.jsonl", "a") as handle:
        handle.write("".join(json.dumps(r) + "\n" for r in rows))
    recent = read_model.flow_in_flight(store, flow_id)["recent_turns"]
    assert [r["turn"] for r in recent] == list(range(16, 26))
    assert recent[-1] == {"turn": 25, "harness_session": 3, "decision": "next",
                          "stop_reason": "end_turn", "elapsed_sec": 1500.0,
                          "changed_total": 25, "context_used": 25000,
                          "at": "2026-11-27T11:25:00+00:00"}


def test_recent_turns_of_a_short_attempt_keep_unknown_facts_null(repo):
    store, flow_id, directory = _flow(repo)
    _running_step(store, flow_id, directory, repo)
    recent = read_model.flow_in_flight(store, flow_id)["recent_turns"]
    assert [r["turn"] for r in recent] == [1, 2, 3]
    assert recent[2] == {"turn": 3, "harness_session": 2, "decision": "next",
                         "stop_reason": "max_tokens", "elapsed_sec": None, "changed_total": None,
                         "context_used": None, "at": "2026-11-27T10:12:00+00:00"}


def test_a_large_progress_file_is_read_by_its_tail_only(repo, monkeypatch):
    import os

    store, flow_id, directory = _flow(repo)
    _running_step(store, flow_id, directory, repo)
    filler = "x" * 900
    with open(directory / "progress" / "build-2.jsonl", "a") as handle:
        for n in range(4, 4004):
            handle.write(json.dumps({"turn": n, "decision": "next", "note": filler}) + "\n")
    size = (directory / "progress" / "build-2.jsonl").stat().st_size
    assert size > 8 * read_model._PROGRESS_TAIL
    reads = []
    real = os.fdopen

    class Spy:
        def __init__(self, handle):
            self.handle = handle

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.handle.close()

        def __getattr__(self, name):
            return getattr(self.handle, name)

        def read(self, n=-1):
            data = self.handle.read(n)
            reads.append(len(data))
            return data

    monkeypatch.setattr(os, "fdopen", lambda *a, **k: Spy(real(*a, **k)))
    live = read_model.flow_in_flight(store, flow_id)
    assert [r["turn"] for r in live["recent_turns"]] == list(range(3994, 4004))
    assert live["turns"] == 4003
    assert reads and max(reads) <= read_model._PROGRESS_TAIL and sum(reads) < size // 4


# --- the status file: written by the adapter's callback, flows only ----------------------

def test_the_status_writer_is_atomic_owner_only_and_rate_limited(tmp_path):
    import stat

    from garuda.flows import live_status

    now = [100.0]
    target = live_status.path(tmp_path, "build", 1)
    writer = live_status.StatusWriter(target, clock=lambda: now[0])
    writer(True, False)
    assert live_status.read(target)["tool_in_flight"] is True and writer.writes == 1
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert stat.S_IMODE(target.parent.stat().st_mode) == 0o700
    now[0] = 100.3
    writer(False, False)  # inside the second: held, not written
    assert live_status.read(target)["tool_in_flight"] is True and writer.writes == 1
    now[0] = 100.5
    writer(True, False)  # back to what is written: nothing to write
    assert writer.writes == 1
    now[0] = 101.2
    writer(False, True)
    assert live_status.read(target) == {"tool_in_flight": False, "approval_parked": True,
                                        "updated_at": live_status.read(target)["updated_at"]}
    assert writer.writes == 2
    writer(False, True)  # unchanged: never rewritten
    assert writer.writes == 2
    assert [p.name for p in target.parent.iterdir()] == [target.name]  # no temporary left


async def test_a_held_change_is_written_when_its_second_is_up(tmp_path):
    import asyncio

    from garuda.flows import live_status

    target = live_status.path(tmp_path, "build", 1)
    writer = live_status.StatusWriter(target, min_interval=0.2)
    writer(True, False)
    writer(False, False)
    writer(False, True)
    assert writer.writes == 1 and live_status.read(target)["tool_in_flight"] is True
    await asyncio.sleep(0.5)
    assert writer.writes == 2
    assert live_status.read(target)["approval_parked"] is True


def test_a_malformed_status_file_is_unknown(tmp_path):
    from garuda.flows import live_status

    target = live_status.path(tmp_path, "build", 1)
    target.parent.mkdir(parents=True)
    for raw in ("not json", json.dumps({"tool_in_flight": 1, "approval_parked": False,
                                        "updated_at": "x"}), "{" + " " * 8192 + "}"):
        target.write_text(raw)
        assert live_status.read(target) is None


async def test_the_adapter_reports_tool_and_approval_state_only_when_asked():
    import asyncio

    from tests.test_step_liveness import _adapter

    seen = []
    runtime = _adapter("long-tool", "--tool-seconds", "1")
    runtime.report_status(lambda tool, parked: seen.append((tool, parked)))
    await runtime.start(task="t", session_id=str(uuid.uuid4()))
    assert await asyncio.wait_for(runtime.prompt("work"), 20) == 1
    await runtime.close()
    assert (True, False) in seen and seen[-1] == (False, False)

    async def slow_person(_action):
        await asyncio.sleep(0.5)
        return True

    seen.clear()
    runtime = _adapter("parked-approval", approval_handler=slow_person)
    runtime.report_status(lambda tool, parked: seen.append((tool, parked)))
    await runtime.start(task="t", session_id=str(uuid.uuid4()))
    assert await asyncio.wait_for(runtime.prompt("work"), 20) == 1
    await runtime.close()
    assert any(parked for _tool, parked in seen) and seen[-1] == (False, False)

    # A raising callback never fails the turn.
    runtime = _adapter("long-tool", "--tool-seconds", "0")
    runtime.report_status(lambda *_a: 1 / 0)
    await runtime.start(task="t", session_id=str(uuid.uuid4()))
    assert await asyncio.wait_for(runtime.prompt("work"), 20) == 1
    await runtime.close()


@pytest.mark.parametrize("limits, written", [
    ({"silence_min": 0.01, "tool_silence_min": 1}, True),
    (None, False),
])
async def test_only_a_flow_step_that_asked_writes_a_status_file(
        repo, tmp_path, monkeypatch, limits, written):
    import asyncio

    from garuda.acp.adapter import AcpRuntime
    from garuda.flows import live_status
    from tests.test_runtime_cli import _install_shim, _on_path, _trusted_settings
    from tests.test_step_liveness import _acp_runner

    asked = []
    real = AcpRuntime.report_status
    monkeypatch.setattr(AcpRuntime, "report_status",
                        lambda self, cb: (asked.append(cb), real(self, cb))[1])
    _on_path(monkeypatch, tmp_path / "bin")
    _install_shim(tmp_path / "bin", profile="long-tool", extra=("--tool-seconds", "1"))
    _trusted_settings(tmp_path, monkeypatch)
    result = await asyncio.wait_for(_acp_runner(repo, limits).run(), 60)
    assert result.completed, result.stopped
    target = live_status.path(engine.flow_dir(SessionStore(), result.flow_session), "work", 1)
    assert bool(asked) is written and target.exists() is written
    if written:
        # The turn ended: nothing is in flight.
        assert live_status.read(target)["tool_in_flight"] is False


def test_a_step_launch_has_no_status_file_by_default():
    assert engine.StepLaunch("f", "s", "r", None, "p", "/w", None, 1, False).status_path is None
    from garuda.flows.launch import status_report

    assert status_report(None) == {}
