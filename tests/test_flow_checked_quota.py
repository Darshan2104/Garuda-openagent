"""A checked flow waits out a Claude usage limit and continues under its checks.

The step attempts run through the checked (owned) launch contract with a fake
launcher that returns the owned worker's closure, as ``tests/test_flows.py``
does; the limited attempt's step session records the typed Claude limit
(``AcpUsageLimitError`` with its validated reset), as the real ACP launcher
does. The engine, journal, receipts, evidence, the original check and the CLI
run for real; only the wait's clock and sleep are replaced.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import uuid
from types import SimpleNamespace

import pytest

from garuda.config import garuda_yaml as gy
from garuda.core.sessions import SessionStore
from garuda.flows import checked_quota, engine
from garuda.flows.engine import QUOTA_NOTE, StepResult
from garuda.flows.quota_recovery import CLAUDE_RESET_MARGIN_SEC
from tests.test_flows import _block
from tests.test_flows import repo as _repo

repo = _repo
pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="owned flow checks require Linux")
RESETS_AT = 1795705200
RESET_TEXT = "2026-11-26 15:00 UTC"


def _config(long_run: bool) -> str:
    build = "{id: build, role: coder, inputs: [plan], outputs: [patch]" + (
        ", long_run: {}}" if long_run else "}")
    return ("version: 1\nroles:\n  planner: {harness: native}\n  coder: {harness: claude}\n"
            "flows:\n  checked:\n    steps:\n"
            "    - {id: plan, role: planner, outputs: [plan]}\n"
            f"    - {build}\n")


@pytest.fixture
def checked(repo, tmp_path, monkeypatch):
    from tests.test_runtime_cli import _install_shim, _on_path, _trusted_settings

    _install_shim(tmp_path / "bin", profile="claude-usage-limit-legacy")
    _on_path(monkeypatch, tmp_path / "bin")
    _trusted_settings(tmp_path, monkeypatch)
    gy.user_path().parent.mkdir(parents=True, exist_ok=True)
    gy.user_path().write_text(_config(False))
    clock = SimpleNamespace(now=RESETS_AT - 3600.0)
    sleeps: list[float] = []
    script = SimpleNamespace(limited={("build", 1)}, reset=RESETS_AT, resets={}, failed=set(), closed=True,
                             launches=[], prompts={}, sleep=None)

    async def sleep(seconds):
        sleeps.append(seconds)
        if script.sleep is not None:
            await script.sleep(seconds)
        clock.now += seconds

    monkeypatch.setattr(checked_quota, "time", SimpleNamespace(time=lambda: clock.now))
    monkeypatch.setattr(checked_quota, "_sleep", sleep)

    async def launcher(launch):
        assert launch.require_owned
        key = (launch.step_id, launch.attempt)
        script.launches.append(key)
        script.prompts[key] = launch.prompt
        session = str(uuid.uuid4())
        lease = launch.capability._parent.snapshot_ownership()["lease"]
        info = repo.stat()
        closure = {"version": 1, "request_digest": "a" * 64, "session_id": session,
                   "parent_id": launch.flow_session, "step_id": launch.step_id,
                   "attempt": launch.attempt, "workspace_identity": [info.st_dev, info.st_ino],
                   "parent_owner": {k: lease[k] for k in ("pid", "identity", "pgid", "epoch")},
                   "process": {"cleanup": {"backend": "linux-subreaper", "echild": True},
                               "outcome": "exited", "supervisor": {}, "command": {}, "exit_code": 0}}
        if key in script.limited:
            assert launch.role_plan.runtime_id == "claude"
            store = SessionStore()
            store.begin(session, task="t", model="m", agent="a", workspace=launch.workspace)
            limit = {"status": "rejected", "rate_limit_type": "five_hour"}
            reset = script.resets.get(key, script.reset)
            if reset is not None:
                limit["resets_at"] = reset
            store.update_meta(session, {
                "error_code": "AcpUsageLimitError", "error_limit": limit,
                "error_detail": "harness.limit_reached: You've hit your limit (agent error -32603)"})
            (repo / "a.txt").write_text(f"partial {launch.attempt}\n")
            if not script.closed:
                closure["process"]["cleanup"] = {"backend": "linux-subreaper", "echild": False}
            return StepResult(session, False, "", closure,
                              error="harness.limit_reached: You've hit your limit")
        if key in script.failed:
            return StepResult(session, False, "", closure, error="acp.timeout during initialize")
        if launch.step_id == "build":
            (repo / "a.txt").write_text("a\n")
        output = _block("plan", "edit a.txt") if launch.step_id == "plan" else _block("patch", "a.txt")
        return StepResult(session, True, output, closure)

    monkeypatch.setattr("garuda.flows.launch.launch_step", launcher)
    return SimpleNamespace(repo=repo, clock=clock, sleeps=sleeps, script=script)


def _run(monkeypatch, capsys, h):
    from tests.test_runtime_cli import _main

    return _main(monkeypatch, capsys, "flow", "run", "checked", "-t", "ship it",
                 "--workspace", str(h.repo), "--check", "true")


def _flow_id(out):
    return json.loads(next(line for line in out.splitlines() if line.startswith("{")))


def _assert_checked_completion(store, flow_id, expected):
    meta = store.load_meta(flow_id)
    assert meta["flow_state"] == "completed"
    assert meta["state"]["verification"] == {"status": "passed", "authority": "user-request"}
    assert len(meta["acceptance_receipts"]) == 1
    rows = engine.receipts(store, flow_id)
    assert [(r["step"], r["attempt"]) for r in rows] == expected
    entries = engine.journal(store, flow_id)
    assert len(entries) == 2 * len(rows)  # exactly one intent and one receipt per attempt
    assert [(e["step"], e["attempt"], e["event"]) for e in entries] == [
        (step, attempt, event) for step, attempt in expected for event in ("intent", "receipt")]
    manifest = json.loads((engine.flow_dir(store, flow_id) / "evidence" / "manifest.json").read_text())
    assert [(row["step"], row["attempt"]) for row in manifest["segments"]] == expected
    assert meta.get(checked_quota.WAIT_KEY) is None
    return meta, rows


@pytest.mark.parametrize("long_run", [False, True], ids=["plain", "long_run"])
def test_a_checked_flow_waits_for_the_reset_and_continues_under_its_checks(
        checked, monkeypatch, capsys, long_run):
    h = checked
    gy.user_path().write_text(_config(long_run))
    code, out = _run(monkeypatch, capsys, h)
    assert code == 0, out
    assert h.script.launches == [("plan", 1), ("build", 1), ("build", 2)]
    # It waited until the reported reset plus the unchecked path's margin, no less.
    assert sum(h.sleeps) == pytest.approx(3600 + CLAUDE_RESET_MARGIN_SEC)
    assert h.clock.now >= RESETS_AT + CLAUDE_RESET_MARGIN_SEC
    assert max(h.sleeps) <= checked_quota.POLL_SEC
    assert f"paused on the Claude five_hour limit until {RESET_TEXT}" in out
    flow_id = _flow_id(out)["flow_session"]
    store = SessionStore()
    _, rows = _assert_checked_completion(store, flow_id, [("plan", 1), ("build", 1), ("build", 2)])
    stopped = rows[1]
    assert stopped["status"] == "stopped" and stopped["stop"]["code"] == "flow.quota_wait"
    assert stopped["stop"]["cause"]["code"] == "AcpUsageLimitError"
    assert stopped["stop"]["cause"]["limit"] == {"status": "rejected", "resets_at": RESETS_AT,
                                                 "rate_limit_type": "five_hour"}
    assert stopped["closure"]["process"]["cleanup"] == {"backend": "linux-subreaper", "echild": True}
    # The continued attempt is told it continues after a quota pause.
    prompt = h.script.prompts[("build", 2)]
    if long_run:
        assert "attempt 1 stopped (flow.quota_wait) because its harness reached its usage limit" in prompt
        assert f"the limit resets at {RESET_TEXT}" in prompt
        assert QUOTA_NOTE not in prompt
    else:
        assert QUOTA_NOTE in prompt
    assert QUOTA_NOTE not in h.script.prompts[("build", 1)]


def test_a_limit_hit_again_waits_again_for_the_later_reset(checked, monkeypatch, capsys):
    h = checked
    later = RESETS_AT + 5 * 3600
    h.script.limited = {("build", 1), ("build", 2)}
    h.script.resets = {("build", 2): later}  # the next window's reset
    code, out = _run(monkeypatch, capsys, h)
    assert code == 0, out
    assert h.script.launches == [("plan", 1), ("build", 1), ("build", 2), ("build", 3)]
    assert h.clock.now >= later + CLAUDE_RESET_MARGIN_SEC
    flow_id = _flow_id(out)["flow_session"]
    _assert_checked_completion(SessionStore(), flow_id,
                               [("plan", 1), ("build", 1), ("build", 2), ("build", 3)])


def test_a_resumed_checked_flow_that_hits_the_limit_waits_too(checked, monkeypatch, capsys):
    from tests.test_runtime_cli import _main

    h = checked
    h.script.failed, h.script.limited = {("build", 1)}, {("build", 2)}
    code, out = _run(monkeypatch, capsys, h)
    assert code == 3 and _flow_id(out)["code"] == "flow.step_failed", out
    assert h.sleeps == []
    flow_id = _flow_id(out)["flow_session"]
    code, out = _main(monkeypatch, capsys, "flow", "resume", flow_id)
    assert code == 0, out
    assert sum(h.sleeps) == pytest.approx(3600 + CLAUDE_RESET_MARGIN_SEC)
    _assert_checked_completion(SessionStore(), flow_id,
                               [("plan", 1), ("build", 1), ("build", 2), ("build", 3)])


def test_a_stale_repeated_reset_never_loops(checked, monkeypatch, capsys):
    """A limit hit again with no later reset is left as the plain resumable stop."""
    h = checked
    h.script.limited = {("build", 1), ("build", 2)}
    code, out = _run(monkeypatch, capsys, h)
    assert code == 3, out
    assert h.script.launches == [("plan", 1), ("build", 1), ("build", 2)]
    stopped = _flow_id(out)
    assert stopped["code"] == "flow.quota_wait"
    meta = SessionStore().load_meta(stopped["flow_session"])
    assert meta["flow_state"] == "stopped" and isinstance(meta["flow_resume_checkpoint"], dict)


def _dead_owner():
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    return {"pid": child.pid, "identity": "gone", "pgid": child.pid, "epoch": "e" * 32}


def test_killed_while_waiting_flow_resume_continues_it(checked, monkeypatch, capsys):
    from garuda.core.read_model import flow_pause
    from tests.test_runtime_cli import _main

    h = checked

    async def killed(_seconds):
        raise asyncio.CancelledError  # the waiting process stops before the reset

    h.script.sleep = killed
    code, out = _run(monkeypatch, capsys, h)
    assert code == 3, out
    stopped = _flow_id(out)
    flow_id = stopped["flow_session"]
    assert stopped["code"] == "flow.quota_wait"
    assert h.script.launches == [("plan", 1), ("build", 1)]
    store = SessionStore()
    meta = store.load_meta(flow_id)
    assert meta["flow_state"] == "stopped" and meta.get(checked_quota.WAIT_KEY) is None
    pause = flow_pause(meta)
    assert pause["kind"] == "quota" and pause["resume"] == "manual"
    assert pause["reset_at"] == RESETS_AT

    # A hard kill leaves its wait record behind, naming a dead owner.
    store.update_meta(flow_id, {checked_quota.WAIT_KEY: {
        "step": "build", "reset_at": RESETS_AT, "owner": _dead_owner()}})
    assert flow_pause(store.load_meta(flow_id))["resume"] == "manual"
    # A live waiter reads as resuming by itself, and a resume beside it is refused.
    from garuda.runtime.ownership import current_owner

    store.update_meta(flow_id, {checked_quota.WAIT_KEY: {
        "step": "build", "reset_at": RESETS_AT, "owner": current_owner().to_dict()}})
    live = flow_pause(store.load_meta(flow_id))
    assert live["resume"] == "automatic" and live["action"] == "resumes after the reset"
    code, out = _main(monkeypatch, capsys, "flow", "resume", flow_id)
    assert code == 2 and out.startswith("Error: flow.quota_wait: process "), out
    assert h.script.launches == [("plan", 1), ("build", 1)]
    store.update_meta(flow_id, {checked_quota.WAIT_KEY: {
        "step": "build", "reset_at": RESETS_AT, "owner": _dead_owner()}})

    # Before the reset, `flow resume` waits for it first, then continues.
    h.script.sleep = None
    h.sleeps.clear()
    code, out = _main(monkeypatch, capsys, "flow", "resume", flow_id)
    assert code == 0, out
    assert sum(h.sleeps) == pytest.approx(RESETS_AT + CLAUDE_RESET_MARGIN_SEC - (RESETS_AT - 3600))
    assert h.script.launches == [("plan", 1), ("build", 1), ("build", 2)]
    assert QUOTA_NOTE in h.script.prompts[("build", 2)]
    _assert_checked_completion(store, flow_id, [("plan", 1), ("build", 1), ("build", 2)])


def test_after_the_reset_flow_resume_continues_without_waiting(checked, monkeypatch, capsys):
    from tests.test_runtime_cli import _main

    h = checked

    async def killed(_seconds):
        raise asyncio.CancelledError

    h.script.sleep = killed
    code, out = _run(monkeypatch, capsys, h)
    flow_id = _flow_id(out)["flow_session"]
    h.script.sleep = None
    h.sleeps.clear()
    h.clock.now = RESETS_AT + CLAUDE_RESET_MARGIN_SEC + 1
    code, out = _main(monkeypatch, capsys, "flow", "resume", flow_id)
    assert code == 0, out
    assert h.sleeps == []
    _assert_checked_completion(SessionStore(), flow_id, [("plan", 1), ("build", 1), ("build", 2)])


def test_without_a_reported_reset_the_stop_is_a_plain_resumable_stop(checked, monkeypatch, capsys):
    from tests.test_runtime_cli import _main

    h = checked
    h.script.reset = None
    code, out = _run(monkeypatch, capsys, h)
    assert code == 3, out
    stopped = _flow_id(out)
    assert stopped["code"] == "flow.quota_wait"
    assert h.sleeps == [] and h.script.launches == [("plan", 1), ("build", 1)]
    code, out = _main(monkeypatch, capsys, "flow", "resume", stopped["flow_session"])
    assert code == 0, out
    assert h.sleeps == []
    _assert_checked_completion(SessionStore(), stopped["flow_session"],
                               [("plan", 1), ("build", 1), ("build", 2)])


def test_a_limit_whose_tree_was_not_proved_closed_is_never_continued(checked, monkeypatch, capsys):
    h = checked
    h.script.closed = False
    code, out = _run(monkeypatch, capsys, h)
    assert code == 3, out
    stopped = _flow_id(out)
    assert stopped["code"] == "flow.cleanup_unproved"
    assert h.sleeps == [] and h.script.launches == [("plan", 1), ("build", 1)]
    meta = SessionStore().load_meta(stopped["flow_session"])
    assert meta["flow_state"] == "quarantined" and "flow_resume_checkpoint" not in meta
    assert checked_quota.pending(SessionStore(), stopped["flow_session"]) is None


def _settled_fixture(monkeypatch, **changes):
    """A synthetic settled checked limit stop, for the eligibility rules alone."""
    receipt = {"step": "build", "attempt": 1, "status": "stopped", "index": 1,
               "role_plan": {"runtime_id": "claude"},
               "stop": {"code": "flow.quota_wait", "cause": {
                   "code": "AcpUsageLimitError",
                   "limit": {"resets_at": RESETS_AT, "rate_limit_type": "five_hour"}}},
               "closure": {"process": {"cleanup": {"backend": "linux-subreaper", "echild": True},
                                       "outcome": "exited"}},
               "evidence_boundary": {"boundary_digest": "b" * 64}}
    meta = {"flow_state": "stopped", "status": "failed",
            "publication_policy": {"kind": "flow-verification"},
            "flow_resume_checkpoint": {"id": "c1"},
            "flow_stop": {"code": "flow.quota_wait", "step": "build"}}
    for key, value in changes.items():
        target, _, field = key.partition("__")
        (receipt if target == "receipt" else meta)[field] = value
    monkeypatch.setattr(engine, "receipts", lambda store, sid: [receipt])
    monkeypatch.setattr(engine, "journal", lambda store, sid: [
        {"event": "intent", "step": "build", "attempt": 1},
        {"event": "receipt", "step": "build", "attempt": 1}])
    monkeypatch.setattr(engine, "unsettled_attempts", lambda store, sid: [])
    return SimpleNamespace(load_meta=lambda sid: meta)


def test_only_a_settled_closed_claude_limit_with_a_future_reset_is_waited_out(monkeypatch):
    now = RESETS_AT - 60
    store = _settled_fixture(monkeypatch)
    pause = checked_quota.pending(store, "f", now=now)
    assert pause is not None and pause.resume_at == RESETS_AT + CLAUDE_RESET_MARGIN_SEC
    assert pause.checkpoint == "c1" and (pause.step, pause.attempt) == ("build", 1)
    assert checked_quota.pending(store, "f", now=RESETS_AT) is None  # reset passed
    assert checked_quota.pending(store, "f", now=now, after=RESETS_AT) is None  # not later
    refused = {
        "dashboard": {"meta__flow_operation": {"operation_id": "o"}},
        "unchecked": {"meta__publication_policy": None},
        "no checkpoint": {"meta__flow_resume_checkpoint": None},
        "not stopped": {"meta__flow_state": "quarantined"},
        "other stop": {"meta__flow_stop": {"code": "flow.step_failed", "step": "build"}},
        "codex": {"receipt__role_plan": {"runtime_id": "codex"}},
        "not proved closed": {"receipt__closure": {"process": {
            "cleanup": {"backend": "linux-subreaper", "echild": False}, "outcome": "exited"}}},
        "no evidence": {"receipt__evidence_boundary": None},
        "no limit": {"receipt__stop": {"code": "flow.quota_wait", "cause": {"code": "x"}}},
    }
    for name, change in refused.items():
        store = _settled_fixture(monkeypatch, **change)
        assert checked_quota.pending(store, "f", now=now) is None, name
    store = _settled_fixture(monkeypatch)  # a later attempt launched and never settled
    monkeypatch.setattr(engine, "unsettled_attempts", lambda store, sid: [("build", 2)])
    assert checked_quota.pending(store, "f", now=now) is None
