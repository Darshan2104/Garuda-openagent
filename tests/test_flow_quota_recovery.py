"""Recovery owner contract: durable flow progress, quota and single dispatch.

Fake only external harness execution and the official quota probe; the service,
engine, artifacts, leases, journals and recovery records all run normally.
"""

import asyncio
import json
import subprocess
import sys
import uuid
from types import SimpleNamespace

import pytest
import yaml

from garuda.config.garuda_yaml import user_path
from garuda.core import read_model
from garuda.core.sessions import SessionStore
from garuda.flows.engine import FlowRunner, StepResult
from garuda.flows.quota_recovery import FlowRecovery, RecoveryStore, default_root
from garuda.flows.service import FlowExecutionService
from garuda.observability.limits import LimitObservation, Window
from garuda.runtime import strict_store as ss
from garuda.runtime.roles import RolePlan
from tests.test_flow_review import APPROVE, CHANGES, _config
from tests.test_flows import _block
from tests.test_flows import repo as _repo

repo = _repo


@pytest.fixture
def harness(repo, monkeypatch):
    config = _config(max_rounds=1)
    user_path().parent.mkdir(parents=True, exist_ok=True)
    user_path().write_text(yaml.safe_dump(config))
    records = RecoveryStore()
    ss.write_document(records.root / "settings.json", {
        "version": 1, "enabled": True, "codex_executable": "/usr/bin/codex"})
    clock = SimpleNamespace(now=1_800_000_000.0)
    monkeypatch.setattr("garuda.flows.quota_recovery.time", SimpleNamespace(time=lambda: clock.now))
    script = SimpleNamespace(sequence=[], fail=("review", 1), failures=1,
                             exhausted=False, reviews=[APPROVE], error="usage limit reached",
                             account="acct:test", version="0.163.0", closure="closed")

    def plan(runner, role):
        spec = runner.resolved.config["roles"][role]
        return RolePlan(role, "codex", "acp", model_id=spec["model_id"],
                        digest=spec["model_id"], write_policy=spec.get("write_policy", "edits"))

    async def probe(_recovery):
        return LimitObservation("codex", script.version, clock.now,
            "codex.account.rateLimits.read", account_digest=script.account,
            windows=(Window("primary", 1.0 if script.exhausted else 0.1, clock.now + 1800, 300),
                     Window("secondary", 0.1, clock.now + 604800, 10080)),
            reached=script.exhausted, reset_at=clock.now + 1800 if script.exhausted else None,
            ordinary_usage_allowed=not script.exhausted)

    async def launch(request):
        pair = (request.step_id, request.attempt)
        script.sequence.append(pair)
        if pair == script.fail and script.failures:
            script.failures -= 1
            script.exhausted = True
            if request.step_id == "build":
                (repo / "partial.txt").write_text("retained partial work")
            return StepResult(str(uuid.uuid4()), False, "", error=script.error,
                              closure={"status": script.closure})
        if request.step_id == "plan":
            output = _block("plan", "edit a.txt")
        elif request.step_id == "build":
            if (repo / "partial.txt").exists():
                assert (repo / "partial.txt").read_text() == "retained partial work"
                assert "Preserve partial edits" in request.prompt
            (repo / "a.txt").write_text(f"build {request.attempt}\n")
            output = _block("patch", f"a.txt build {request.attempt}")
        else:
            output = _block("review", script.reviews.pop(0))
        return StepResult(str(uuid.uuid4()), True, output, closure={"status": "closed"})

    monkeypatch.setattr(FlowRunner, "_role_plan", plan)
    monkeypatch.setattr(FlowRecovery, "probe", probe)
    monkeypatch.setattr("garuda.flows.launch.launch_step", launch)
    return SimpleNamespace(repo=repo, script=script, clock=clock, records=records,
                           service=FlowExecutionService(), probe=probe)


@pytest.mark.parametrize("step", ["plan", "build", "review"])
async def test_hourly_recovery_retains_completed_work_and_continues_pending_step(harness, step):
    h = harness
    h.script.fail = (step, 1)
    first = await h.service.run("pbr", "ship it", str(h.repo))
    sid = first.flow.flow_session
    assert first.flow.stopped.code == "flow.step_failed"
    assert h.records.get(sid)["state"] == "waiting-for-quota"
    before = list(h.script.sequence)
    h.clock.now += 3601
    waiting = await FlowRecovery().tick()
    assert waiting["flows"][0]["state"] == "waiting-for-quota"
    assert h.script.sequence == before

    h.script.exhausted = False
    h.clock.now += 3601
    outcome = await FlowRecovery().tick()
    assert outcome["flows"][0]["state"] == "completed"
    assert h.records.get(sid)["dispatches"] == 1
    assert h.script.sequence.count(("plan", 1)) == 1
    if step == "review":
        assert [p for p in h.script.sequence if p[0] == "build"] == [("build", 1)]
    if step == "build":
        assert (h.repo / "partial.txt").read_text() == "retained partial work"
    meta = SessionStore().load_meta(sid)
    assert meta["status"] == "completed" and meta["review"]["status"] == "review_approved"
    assert read_model.session(SessionStore(), sid)["flow"]["quota_recovery"]["state"] == "completed"
    await FlowRecovery().tick()
    assert h.records.get(sid)["dispatches"] == 1


@pytest.mark.parametrize("step", ["build", "review"])
async def test_a_quota_pause_preserves_review_feedback_and_round_budget(harness, step):
    h = harness
    h.script.fail = (step, 2)
    h.script.reviews = [CHANGES, CHANGES]
    first = await h.service.run("pbr", "ship it", str(h.repo))
    sid = first.flow.flow_session
    checkpoint = SessionStore().load_meta(sid)["flow_continuation"]
    assert checkpoint["round"] == 1 and len(checkpoint["history"]) == 1
    h.script.exhausted = False
    h.clock.now += 3601
    await FlowRecovery().tick()
    meta = SessionStore().load_meta(sid)
    assert meta["flow_stop"]["code"] == "flow.review_changes_requested"
    assert meta["review"]["rounds"] == 2
    assert h.records.get(sid)["state"] == "blocked"
    if step == "review":
        assert [p for p in h.script.sequence if p[0] == "build"] == [("build", 1), ("build", 2)]


@pytest.mark.parametrize("change", ["workspace", "configuration", "account", "version", "receipt"])
async def test_changed_continuation_evidence_never_dispatches_a_step(harness, change):
    h = harness
    first = await h.service.run("pbr", "ship it", str(h.repo))
    sid = first.flow.flow_session
    before = list(h.script.sequence)
    if change == "workspace":
        (h.repo / "a.txt").write_text("user work during pause")
    elif change == "configuration":
        user_path().write_text(user_path().read_text() + "\n# changed\n")
    elif change == "receipt":
        path = SessionStore().session_dir(sid) / "flow" / "receipts" / "build-1.json"
        data = json.loads(path.read_text())
        data["session_id"] = "replaced"
        path.chmod(0o600)
        path.write_text(json.dumps(data))
    elif change == "account":
        h.script.account = "acct:other"
    else:
        h.script.version = "999.1.0"
    h.script.exhausted = False
    h.clock.now += 3601
    await FlowRecovery().tick()
    assert h.script.sequence == before
    assert h.records.get(sid)["state"] in {"blocked", "waiting-for-quota"}


@pytest.mark.parametrize("failure", ["permission denied; usage limit reached", "Internal error", "unproved cleanup"])
async def test_nonquota_or_unclosed_failures_are_visible_and_never_retried(harness, failure):
    h = harness
    if failure == "unproved cleanup":
        h.script.closure = "unknown"
    else:
        h.script.error = failure
    first = await h.service.run("pbr", "ship it", str(h.repo))
    sid = first.flow.flow_session
    assert h.records.get(sid)["state"] == "blocked"
    before = list(h.script.sequence)
    h.script.exhausted = False
    h.clock.now += 3601
    await FlowRecovery().tick()
    assert h.script.sequence == before


async def test_cancel_or_disable_prevents_automatic_continuation(harness):
    h = harness
    first = await h.service.run("pbr", "ship it", str(h.repo))
    sid = first.flow.flow_session
    before = list(h.script.sequence)
    ss.write_document(default_root() / "settings.json", {"version": 1, "enabled": False})
    h.script.exhausted = False
    h.clock.now += 3601
    assert (await FlowRecovery().tick())["enabled"] is False
    h.records.cancel(sid)
    with pytest.raises(ValueError, match="not waiting"):
        await h.service.resume(sid)
    assert h.script.sequence == before


async def test_only_one_of_competing_ticks_can_dispatch(harness):
    h = harness
    first = await h.service.run("pbr", "ship it", str(h.repo))
    sid = first.flow.flow_session
    h.script.exhausted = False
    h.clock.now += 3601
    await asyncio.gather(FlowRecovery().tick(), FlowRecovery().tick())
    assert h.records.get(sid)["state"] == "completed"
    assert [p for p in h.script.sequence if p[0] == "review"] == [("review", 1), ("review", 2)]
    with pytest.raises(ValueError, match="not waiting"):
        await h.service.resume(sid)


async def test_a_lost_dispatch_reservation_is_uncertain_and_never_replayed(harness, monkeypatch):
    h = harness
    first = await h.service.run("pbr", "ship it", str(h.repo))
    sid = first.flow.flow_session
    h.records.claim(sid, automatic=True)
    monkeypatch.setattr("garuda.flows.quota_recovery.owner_liveness", lambda owner: False)
    before = list(h.script.sequence)
    await FlowRecovery().tick()
    assert h.records.get(sid)["state"] == "uncertain"
    assert read_model.session(SessionStore(), sid)["quota_recovery"]["state"] == "uncertain"
    assert h.script.sequence == before


async def test_repeated_quota_exhaustion_keeps_the_continuation_enrolled(harness):
    h = harness
    first = await h.service.run("pbr", "ship it", str(h.repo))
    sid = first.flow.flow_session
    h.script.fail, h.script.failures = ("review", 2), 1
    h.script.exhausted = False
    h.clock.now += 3601
    await FlowRecovery().tick()
    assert h.records.get(sid)["state"] == "waiting-for-quota"
    h.script.exhausted = False
    h.clock.now += 3601
    await FlowRecovery().tick()
    assert h.records.get(sid)["state"] == "completed"
    assert [p for p in h.script.sequence if p[0] == "build"] == [("build", 1)]


async def test_manual_resume_racing_a_tick_uses_the_same_exclusive_claim(harness, monkeypatch):
    h = harness
    first = await h.service.run("pbr", "ship it", str(h.repo))
    sid = first.flow.flow_session
    h.script.exhausted = False
    h.clock.now += 3601

    async def concurrent_probe(recovery):
        await asyncio.sleep(0)
        return await h.probe(recovery)

    monkeypatch.setattr(FlowRecovery, "probe", concurrent_probe)
    results = await asyncio.gather(FlowRecovery().tick(), h.service.resume(sid), return_exceptions=True)
    assert any(not isinstance(result, Exception) for result in results)
    assert h.records.get(sid)["state"] == "completed"
    assert h.records.get(sid)["dispatches"] == 1
    assert [p for p in h.script.sequence if p[0] == "review"] == [("review", 1), ("review", 2)]


async def test_session_stop_revokes_a_waiting_quota_continuation(harness):
    from garuda.interfaces.bg_sessions import cancel

    h = harness
    first = await h.service.run("pbr", "ship it", str(h.repo))
    sid = first.flow.flow_session
    assert cancel(SessionStore(), sid) == "cancelled quota continuation"
    h.script.exhausted = False
    h.clock.now += 3601
    await FlowRecovery().tick()
    assert h.records.get(sid)["state"] == "cancelled"
    assert h.script.sequence == [("plan", 1), ("build", 1), ("review", 1)]


async def test_quota_consumed_between_cron_probe_and_launch_waits_again(harness, monkeypatch):
    h = harness
    first = await h.service.run("pbr", "ship it", str(h.repo))
    sid = first.flow.flow_session
    h.script.exhausted = False
    h.clock.now += 3601
    calls = 0

    async def raced_probe(recovery):
        nonlocal calls
        calls += 1
        if calls == 2:
            h.script.exhausted = True
        return await h.probe(recovery)

    monkeypatch.setattr(FlowRecovery, "probe", raced_probe)
    await FlowRecovery().tick()
    assert h.records.get(sid)["state"] == "waiting-for-quota"
    assert h.script.sequence == [("plan", 1), ("build", 1), ("review", 1)]
    h.script.exhausted = False
    h.clock.now += 3601
    await FlowRecovery().tick()
    assert h.records.get(sid)["state"] == "completed"


def test_dispatch_claim_is_exclusive_across_independent_processes(tmp_path):
    root = tmp_path / "recovery"
    records = RecoveryStore(root)
    ss.write_document(root / "settings.json", {"version": 1, "enabled": True})
    records.update("pending-flow", {"state": "waiting-for-quota"})
    script = '''
import sys, time
from pathlib import Path
from garuda.flows.quota_recovery import RecoveryStore
from garuda.runtime import strict_store as ss
original = ss.write_locked_document
def delayed(*args):
    time.sleep(0.15)  # model slow durable publication inside the critical section
    return original(*args)
ss.write_locked_document = delayed
print("ready", flush=True)
input()
try:
    RecoveryStore(Path(sys.argv[1])).claim("pending-flow", automatic=True)
    print("claimed", flush=True)
except ValueError:
    print("refused", flush=True)
'''
    processes = [subprocess.Popen([sys.executable, "-c", script, str(root)],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True) for _ in range(2)]
    try:
        for proc in processes:
            assert proc.stdout.readline().strip() == "ready"
        for proc in processes:
            proc.stdin.write("go\n")
            proc.stdin.flush()
        results = [proc.communicate(timeout=15) for proc in processes]
        assert sorted(output.strip() for output, _error in results) == ["claimed", "refused"]
        assert all(proc.returncode == 0 for proc in processes), results
    finally:
        for proc in processes:
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=15)


async def test_account_change_during_input_preparation_refuses_actual_dispatch(harness, monkeypatch):
    h = harness
    first = await h.service.run("pbr", "ship it", str(h.repo))
    sid = first.flow.flow_session
    h.script.exhausted = False
    original = FlowRunner._inputs

    async def delayed_inputs(runner, step, done):
        result = await original(runner, step, done)
        if step["id"] == "review":
            h.clock.now += 61
            h.script.account = "acct:changed-during-preparation"
        return result

    monkeypatch.setattr(FlowRunner, "_inputs", delayed_inputs)
    await FlowRecovery().tick()
    assert h.records.get(sid)["state"] == "blocked"
    assert h.script.sequence == [("plan", 1), ("build", 1), ("review", 1)]


async def test_quota_is_rechecked_after_a_long_coder_before_starting_review(harness, monkeypatch):
    h = harness
    h.script.fail = ("never", 0)
    from garuda.flows import launch

    original = launch.launch_step

    async def slow_coder(request):
        result = await original(request)
        if request.step_id == "build":
            h.clock.now += 7200
            h.script.exhausted = True
        return result

    monkeypatch.setattr(launch, "launch_step", slow_coder)
    first = await h.service.run("pbr", "ship it", str(h.repo))
    sid = first.flow.flow_session
    assert h.records.get(sid)["state"] == "waiting-for-quota"
    assert first.flow.stopped.code == "flow.quota_wait"
    assert h.script.sequence == [("plan", 1), ("build", 1)]
    h.script.exhausted = False
    await FlowRecovery().tick()
    assert h.records.get(sid)["state"] == "completed"
    assert h.script.sequence == [("plan", 1), ("build", 1), ("review", 1)]


async def test_a_losing_tick_cannot_overwrite_another_ticks_reserved_dispatch(harness, monkeypatch):
    h = harness
    first = await h.service.run("pbr", "ship it", str(h.repo))
    sid = first.flow.flow_session
    h.script.exhausted = False
    both_read, reserved, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    calls = 0
    original_resume = FlowExecutionService.resume

    async def paused_resume(service, *args, **kwargs):
        reserved.set()
        await release.wait()
        return await original_resume(service, *args, **kwargs)

    async def racing_probe(recovery):
        nonlocal calls
        calls += 1
        if calls == 1:
            await both_read.wait()
        elif calls == 2:
            both_read.set()
            await reserved.wait()
        return await h.probe(recovery)

    monkeypatch.setattr(FlowExecutionService, "resume", paused_resume)
    monkeypatch.setattr(FlowRecovery, "probe", racing_probe)
    winner = asyncio.create_task(FlowRecovery().tick())
    loser = asyncio.create_task(FlowRecovery().tick())
    try:
        await asyncio.wait_for(loser, timeout=5)
        assert h.records.get(sid)["state"] == "dispatch-reserved"
        release.set()
        await asyncio.wait_for(winner, timeout=10)
        assert h.records.get(sid)["state"] == "completed"
        assert h.records.get(sid)["dispatches"] == 1
    finally:
        release.set()
        await asyncio.gather(winner, loser, return_exceptions=True)


async def test_tick_cli_reports_a_lost_dispatch_on_stderr_and_nonzero_exit(harness):
    h = harness
    first = await h.service.run("pbr", "ship it", str(h.repo))
    sid = first.flow.flow_session
    claim = h.records.claim(sid, automatic=True)
    h.records.update(sid, {"owner": {"pid": 2_000_000_000, "identity": "gone", "pgid": 1}},
                     token=claim["token"])
    result = subprocess.run([sys.executable, "-m", "garuda.interfaces.main", "recovery", "tick",
                             "--root", str(h.records.root)],
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 3
    assert "requires attention" in result.stderr
    assert json.loads(result.stdout)["flows"][0]["state"] == "uncertain"
    assert h.records.get(sid)["state"] == "uncertain"
