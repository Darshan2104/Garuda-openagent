"""A Claude usage limit pauses a flow and quota recovery resumes it after the reset (#834).

The failing step runs the real ACP launcher against the fake agent's
claude-agent-acp 0.85.0 limit shape (``errorKind`` only; the reset comes from
the turn's ``_claude/rateLimit`` usage snapshot). The engine, receipts, leases
and recovery records run normally; no prompt is ever sent to check a limit.
"""

from __future__ import annotations

import dataclasses
import uuid
from types import SimpleNamespace

import pytest
import yaml

from garuda.config.garuda_yaml import user_path
from garuda.core.sessions import SessionStore
from garuda.flows.engine import LIMIT_CAUSE_CODES, FlowRunner, StepResult
from garuda.flows.quota_recovery import (
    CLAUDE_BACKOFF_SEC,
    CLAUDE_RESET_MARGIN_SEC,
    FlowRecovery,
    RecoveryStore,
)
from garuda.flows.service import FlowExecutionService
from garuda.runtime import strict_store as ss
from garuda.runtime.roles import RolePlan
from tests.test_flow_review import APPROVE, _config
from tests.test_flows import _block
from tests.test_flows import repo as _repo

repo = _repo
RESETS_AT = 1795705200  # the fake agent's `_claude/rateLimit.resetsAt`
RESET_TEXT = "2026-11-26 15:00 UTC"


def _claude_config():
    config = _config(max_rounds=1)
    config["roles"]["coder"] = {"harness": "claude"}
    return config


@pytest.fixture
def claude_flow(repo, tmp_path, monkeypatch):
    from tests.test_runtime_cli import _install_shim, _on_path, _trusted_settings

    _install_shim(tmp_path / "bin", profile="claude-usage-limit-legacy")
    _on_path(monkeypatch, tmp_path / "bin")
    _trusted_settings(tmp_path, monkeypatch)
    user_path().parent.mkdir(parents=True, exist_ok=True)
    user_path().write_text(yaml.safe_dump(_claude_config()))
    records = RecoveryStore()
    ss.write_document(records.root / "settings.json", {"version": 1, "enabled": True})
    clock = SimpleNamespace(now=RESETS_AT - 3600.0)
    monkeypatch.setattr("garuda.flows.quota_recovery.time", SimpleNamespace(time=lambda: clock.now))
    script = SimpleNamespace(sequence=[], limited={("build", 1)}, unknown=False)

    async def never_probe(_recovery):
        raise AssertionError("a Claude limit is never probed")

    async def launch(request):
        from garuda.flows.launch import _acp

        pair = (request.step_id, request.attempt)
        script.sequence.append(pair)
        if pair in script.limited:
            if script.unknown:
                # A typed limit whose harness reported no reset at all.
                sid = str(uuid.uuid4())
                store = request.store
                store.begin(sid, task="t", model="m", agent="a", workspace=request.workspace)
                store.update_meta(sid, {"error_code": "AcpUsageLimitError", "error_detail":
                                        "harness.limit_reached: usage limit reached (agent error -32603)"})
                return StepResult(sid, False, "", error="harness.limit_reached: usage limit reached",
                                  closure={"status": "closed"})
            # The real launcher against the recorded claude-agent-acp 0.85.0 limit shape.
            assert request.role_plan.runtime_id == "claude"
            fake = dataclasses.replace(request, role_plan=RolePlan("coder", "fakeacp", "acp",
                                                                   permissions="auto"))
            return await _acp(fake)
        if request.step_id == "plan":
            output = _block("plan", "edit a.txt")
        elif request.step_id == "build":
            (repo / "a.txt").write_text(f"build {request.attempt}\n")
            output = _block("patch", f"a.txt build {request.attempt}")
        else:
            output = _block("review", APPROVE)
        return StepResult(str(uuid.uuid4()), True, output, closure={"status": "closed"})

    monkeypatch.setattr(FlowRecovery, "probe", never_probe)
    monkeypatch.setattr("garuda.flows.launch.launch_step", launch)
    return SimpleNamespace(repo=repo, script=script, clock=clock, records=records,
                           service=FlowExecutionService())


async def test_a_claude_limit_pauses_the_flow_and_resumes_after_the_reset(claude_flow, capsys,
                                                                          monkeypatch):
    h = claude_flow
    first = await h.service.run("pbr", "ship it", str(h.repo))
    sid = first.flow.flow_session
    assert first.flow.stopped.code == "flow.quota_wait"
    meta = SessionStore().load_meta(sid)
    stop = meta["flow_stop"]
    assert stop["code"] == "flow.quota_wait" and stop["step"] == "build"
    assert stop["cause"]["code"] in LIMIT_CAUSE_CODES
    assert stop["cause"]["limit"] == {"resets_at": RESETS_AT, "rate_limit_type": "five_hour"}
    assert f"resets at {RESET_TEXT}" in stop["message"]
    row = h.records.get(sid)
    assert row["state"] == "waiting-for-quota" and row["quota_source"] == "claude"
    assert row["reset_at"] == RESETS_AT and row["rate_limit_type"] == "five_hour"
    assert row["next_check"] == RESETS_AT + CLAUDE_RESET_MARGIN_SEC
    assert meta["quota_recovery"]["quota_source"] == "claude"
    assert meta["quota_recovery"]["rate_limit_type"] == "five_hour"

    from tests.test_runtime_cli import _main

    code, shown = _main(monkeypatch, capsys, "flow", "show", sid)
    assert code == 0
    assert f"limit: five_hour; resets at {RESET_TEXT}" in shown
    assert f"Claude five_hour limit resets at {RESET_TEXT}" in shown

    # Before the reset (and its margin) the tick does nothing and probes nothing.
    before = list(h.script.sequence)
    h.clock.now = RESETS_AT + CLAUDE_RESET_MARGIN_SEC - 1
    waiting = await FlowRecovery().tick()
    assert waiting["flows"][0]["state"] == "waiting-for-quota"
    assert h.script.sequence == before
    assert h.records.health()["last_result"]["outcome"] == "quota-waiting"

    h.clock.now = RESETS_AT + CLAUDE_RESET_MARGIN_SEC + 1
    done = await FlowRecovery().tick()
    assert done["flows"][0]["state"] == "completed"
    # The pending step ran again as a new attempt; the completed plan did not.
    assert h.script.sequence == [*before, ("build", 2), ("review", 1)]
    assert h.records.get(sid)["dispatches"] == 1
    assert SessionStore().load_meta(sid)["status"] == "completed"
    assert h.records.health()["last_result"]["outcome"] == "dispatched"


async def test_a_resume_before_the_reported_reset_is_refused_without_a_step(claude_flow):
    h = claude_flow
    sid = (await h.service.run("pbr", "ship it", str(h.repo))).flow.flow_session
    before = list(h.script.sequence)
    again = await h.service.resume(sid)
    assert again.flow.stopped.code == "flow.quota_wait"
    assert RESET_TEXT in str(again.flow.stopped)
    assert h.script.sequence == before
    row = h.records.get(sid)
    assert row["state"] == "waiting-for-quota" and row["reset_at"] == RESETS_AT
    assert row["next_check"] == RESETS_AT + CLAUDE_RESET_MARGIN_SEC
    h.clock.now = RESETS_AT + CLAUDE_RESET_MARGIN_SEC + 1
    assert (await FlowRecovery().tick())["flows"][0]["state"] == "completed"


async def test_an_unknown_reset_pauses_and_retries_with_backoff(claude_flow):
    h = claude_flow
    h.script.unknown = True
    h.script.limited = {("build", 1), ("build", 2)}
    start = h.clock.now
    sid = (await h.service.run("pbr", "ship it", str(h.repo))).flow.flow_session
    row = h.records.get(sid)
    assert row["state"] == "waiting-for-quota" and row["reset_at"] is None
    assert row["unknown_reset_pauses"] == 1
    assert row["next_check"] == start + CLAUDE_BACKOFF_SEC[0]
    assert SessionStore().load_meta(sid)["flow_stop"]["code"] == "flow.quota_wait"

    h.clock.now = start + CLAUDE_BACKOFF_SEC[0] - 1
    await FlowRecovery().tick()
    assert ("build", 2) not in h.script.sequence

    h.clock.now = start + CLAUDE_BACKOFF_SEC[0] + 1
    await FlowRecovery().tick()  # tries again; still limited, so it backs off further
    row = h.records.get(sid)
    assert ("build", 2) in h.script.sequence
    assert row["state"] == "waiting-for-quota" and row["unknown_reset_pauses"] == 2
    assert row["next_check"] == h.clock.now + CLAUDE_BACKOFF_SEC[1]

    retried = h.clock.now
    h.clock.now = retried + CLAUDE_BACKOFF_SEC[0] + 1  # past the first wait, not the second
    await FlowRecovery().tick()
    assert ("build", 3) not in h.script.sequence
    h.clock.now = retried + CLAUDE_BACKOFF_SEC[1] + 1
    assert (await FlowRecovery().tick())["flows"][0]["state"] == "completed"
    assert h.script.sequence.count(("plan", 1)) == 1 and ("build", 3) in h.script.sequence


async def test_without_recovery_a_claude_limit_still_pauses_and_flow_resume_reruns_it(claude_flow):
    h = claude_flow
    ss.write_document(h.records.root / "settings.json", {"version": 1, "enabled": False})
    first = await h.service.run("pbr", "ship it", str(h.repo))
    sid = first.flow.flow_session
    assert first.flow.stopped.code == "flow.quota_wait"
    assert h.records.get(sid) is None
    resumed = await h.service.resume(sid)
    assert resumed.flow.stopped is None
    assert h.script.sequence == [("plan", 1), ("build", 1), ("build", 2), ("review", 1)]


def test_a_checked_flow_keeps_its_step_failed_contract():
    plan = RolePlan("coder", "claude", "acp", permissions="auto")
    cause = {"code": "AcpUsageLimitError", "limit": {"resets_at": RESETS_AT}}
    unchecked = SimpleNamespace(verification_request=None)
    assert FlowRunner._limit_pause(unchecked, plan, cause) is True
    assert FlowRunner._limit_pause(SimpleNamespace(verification_request=object()), plan, cause) is False
    codex = RolePlan("coder", "codex", "acp", permissions="auto")
    assert FlowRunner._limit_pause(unchecked, codex, cause) is False
    assert FlowRunner._limit_pause(unchecked, plan, {"code": "acp.remote.internal_error"}) is False
