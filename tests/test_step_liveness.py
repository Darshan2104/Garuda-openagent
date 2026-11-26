"""Liveness for long flow steps (#828): step deadline, silence watchdog, checked stops.

Limits are whole minutes in ``garuda.yaml``; the engine accepts any positive
number of minutes, so these tests give the flow dict fractions of a minute
directly instead of a public seconds key.
"""

import asyncio
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

from garuda.acp.adapter import AcpRuntime
from garuda.acp.protocol import AcpTimeoutError
from garuda.config import garuda_yaml as gy
from garuda.config.garuda_yaml import GarudaConfigError
from garuda.core.sessions import SessionStore
from garuda.flows import engine
from garuda.flows.engine import FlowRunner, StepLimits, StepResult, step_limits
from garuda.workspace.lease import LeaseStore

FAKE = [sys.executable, "-m", "garuda.acp.fake_agent"]
linux = pytest.mark.skipif(sys.platform != "linux", reason="actual Linux role ownership")


def _git(path, *args):
    subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True,
                   env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})


@pytest.fixture
def repo(tmp_path):
    path = tmp_path / "repo"
    path.mkdir()
    _git(path, "init", "-q")
    (path / "a.txt").write_text("a\n")
    _git(path, "add", ".")
    _git(path, "commit", "-qm", "base")
    return path


def _block(kind, text):
    return f'notes\n<garuda-artifact type="{kind}">\n{text}\n</garuda-artifact>'


# --- configuration ---------------------------------------------------------------------

ROLES = {"planner": {"harness": "native", "write_policy": "no-edits"},
         "coder": {"harness": "native"}}


def _flow_config(flow):
    return {"version": 1, "roles": ROLES, "flows": {"f": flow}}


def test_step_and_flow_limits_parse_in_whole_minutes():
    parsed = gy.parse(_flow_config({
        "limits": {"silence_min": 30},
        "steps": [{"id": "plan", "role": "planner", "outputs": ["plan"]},
                  {"id": "build", "role": "coder",
                   "limits": {"deadline_min": 720, "tool_silence_min": 180}}]}))
    flow = gy.resolve(parsed, None).config["flows"]["f"]
    assert flow["limits"] == {"silence_min": 30}
    assert "limits" not in flow["steps"][0]
    assert flow["steps"][1]["limits"] == {"deadline_min": 720, "tool_silence_min": 180}
    # A step's keys override the flow's, key by key; tool silence defaults to 120 min.
    assert step_limits(flow, flow["steps"][0]) == StepLimits(None, 1800.0, 7200.0)
    assert step_limits(flow, flow["steps"][1]) == StepLimits(43200.0, 1800.0, 10800.0)
    assert step_limits({"steps": []}, {"id": "x"}) is None


@pytest.mark.parametrize("limits, path", [
    ({"deadline_min": 0}, "flows.f.steps[0].limits.deadline_min"),
    ({"deadline_min": 7 * 24 * 60 + 1}, "flows.f.steps[0].limits.deadline_min"),
    ({"deadline_min": 1.5}, "flows.f.steps[0].limits.deadline_min"),
    ({"silence_min": True}, "flows.f.steps[0].limits.silence_min"),
    ({"silence_min": 24 * 60 + 1}, "flows.f.steps[0].limits.silence_min"),
    ({"silence_sec": 5}, "flows.f.steps[0].limits.silence_sec"),
    ({}, "flows.f.steps[0].limits"),
    ({"silence_min": 30, "tool_silence_min": 10}, "flows.f.steps[0].limits.tool_silence_min"),
    ("30m", "flows.f.steps[0].limits"),
])
def test_bad_step_limits_are_refused_with_their_full_path(limits, path):
    with pytest.raises(GarudaConfigError) as caught:
        gy.parse(_flow_config({"steps": [{"role": "coder", "limits": limits}]}))
    assert caught.value.path == path


def test_a_step_override_may_not_make_tool_silence_shorter_than_the_flows_silence():
    with pytest.raises(GarudaConfigError) as caught:
        gy.parse(_flow_config({"limits": {"silence_min": 60},
                               "steps": [{"role": "coder", "limits": {"tool_silence_min": 30}}]}))
    assert caught.value.path == "flows.f.steps[0].limits.tool_silence_min"
    with pytest.raises(GarudaConfigError) as caught:
        gy.parse(_flow_config({"limits": {"bogus": 1}, "steps": [{"role": "coder"}]}))
    assert caught.value.path == "flows.f.limits.bogus"


def test_a_flow_without_limits_keeps_its_definition_digest():
    """Checked-flow resume compares this digest; flows without ``limits`` must not change.

    The pinned value was computed on the parent commit, before ``limits`` existed.
    """
    from garuda.flows.quota_recovery import digest

    config = {"version": 1, "roles": ROLES, "flows": {"pb": {
        "description": "plan then build",
        "steps": [{"id": "plan", "role": "planner", "outputs": ["plan"]},
                  {"id": "build", "role": "coder", "outputs": ["patch"], "retries": 1}]}}}
    flow = gy.resolve(gy.parse(config), None).config["flows"]["pb"]
    assert "limits" not in flow and all("limits" not in step for step in flow["steps"])
    assert digest(flow) == "9444e8d36829bcf95b55701db50612514ba4ce660c7f0ae3fa9eeaec918993cf"


# --- the engine: resumable liveness stops ---------------------------------------------

ONE = {"version": 1, "roles": {"coder": {"harness": "native"}},
       "flows": {"one": {"steps": [{"id": "work", "role": "coder", "outputs": ["patch"],
                                    "retries": 2}]}}}


def _one_runner(repo, launcher, limits, **kw):
    resolved = gy.resolve(gy.parse(ONE), None)
    flow = json.loads(json.dumps(resolved.config["flows"]["one"]))
    if limits is not None:
        flow["steps"][0]["limits"] = limits
    return FlowRunner(SessionStore(), repo, "one", flow, resolved, task="t",
                      launcher=launcher, **kw)


@pytest.mark.parametrize("liveness, code", [("deadline", "flow.step_deadline"),
                                            ("silence", "flow.step_silent")])
async def test_a_liveness_stop_is_a_resumable_stopped_receipt(repo, liveness, code):
    launches = []

    async def stopped_then_done(launch):
        launches.append(launch)
        if len(launches) == 1:
            return StepResult(str(uuid.uuid4()), False, "", error="the turn was cancelled",
                              liveness=liveness, stop_reason=None)
        return StepResult(str(uuid.uuid4()), True, _block("patch", "a.txt"))

    limits = {"deadline_min": 0.5, "silence_min": 0.25}
    runner = _one_runner(repo, stopped_then_done, limits)
    result = await runner.run()
    assert result.stopped is not None and result.stopped.code == code
    assert "garuda flow resume" in str(result.stopped)
    # Never retried by `retries`: one launch, one stopped receipt, two journal entries.
    assert len(launches) == 1
    assert launches[0].limits == StepLimits(30.0, 15.0, 7200.0)
    receipt, = result.receipts if result.receipts else engine.receipts(
        SessionStore(), result.flow_session)
    assert receipt["status"] == "stopped" and receipt["stop"]["code"] == code
    assert receipt["limits"] == {"deadline_sec": 30.0, "silence_sec": 15.0,
                                 "tool_silence_sec": 7200.0}
    assert [e["event"] for e in engine.journal(SessionStore(), result.flow_session)] == [
        "intent", "receipt"]
    meta = SessionStore().load_meta(result.flow_session)
    assert meta["flow_state"] == "stopped" and meta["flow_stop"]["code"] == code
    assert LeaseStore().holders_of(repo) == []

    resumed = await _one_runner(repo, stopped_then_done, limits,
                                flow_session=result.flow_session).run(resume=True)
    assert resumed.completed
    rows = engine.receipts(SessionStore(), result.flow_session)
    assert [(r["attempt"], r["status"]) for r in rows] == [(1, "stopped"), (2, "done")]
    assert [e["event"] for e in engine.journal(SessionStore(), result.flow_session)] == [
        "intent", "receipt", "intent", "receipt"]
    assert code in engine.RESUMABLE_CHECKED_STOPS
    assert code not in engine.RETRYABLE_STOPS


async def test_a_step_without_limits_launches_without_them_and_fails_as_before(repo):
    launches = []

    async def failing(launch):
        launches.append(launch)
        return StepResult(str(uuid.uuid4()), False, "", liveness="deadline")

    result = await _one_runner(repo, failing, None).run()
    # Without limits a launcher's liveness claim is not a liveness stop.
    assert result.stopped.code == "flow.step_failed"
    assert all(launch.limits is None for launch in launches)
    assert all("limits" not in r for r in engine.receipts(SessionStore(), result.flow_session))


def test_the_new_codes_are_documented():
    root = Path(__file__).resolve().parents[1]
    text = (root / "docs" / "reference" / "troubleshooting.md").read_text()
    assert "flow.step_deadline" in text and "flow.step_silent" in text


# --- the owned worker: typed result, backstop timeout ---------------------------------

def test_owned_backstop_is_the_deadline_plus_a_margin_and_unchanged_without_one():
    from garuda.runtime.owned_role import owned_timeout

    assert owned_timeout(None) == 3600
    assert owned_timeout(StepLimits(None, 60.0, 7200.0)) == 3600
    assert owned_timeout(StepLimits(36000.0)) == 36000 + 600


def test_role_control_accepts_only_a_bounded_typed_failure():
    from garuda.runtime.role_control import valid_failure

    assert valid_failure({"code": "run.deadline_exceeded", "detail": "x", "liveness": "deadline"})
    assert valid_failure({"stop_reason": "max_turn_requests"})
    assert not valid_failure({"code": "x", "extra": 1})
    assert not valid_failure({"liveness": "forever"})
    assert not valid_failure({"stop_reason": "x" * 65})
    assert not valid_failure({"detail": 5})


async def test_role_control_result_keeps_a_failure_only_on_a_failed_step():
    from garuda.runtime.role_control import RoleControl

    class Guard:
        def _check(self):
            return None

    control = RoleControl.__new__(RoleControl)
    control.guard = Guard()
    control.result = None
    sid = str(uuid.uuid4())
    with pytest.raises(ValueError):
        await control._dispatch("result", {"session_id": sid, "success": True, "output": "",
                                           "failure": {"code": "x"}})
    with pytest.raises(ValueError):
        await control._dispatch("result", {"session_id": sid, "success": False, "output": "",
                                           "failure": {"code": "x"},
                                           "refusal": {"code": "role.x", "message": ""}})
    await control._dispatch("result", {"session_id": sid, "success": False, "output": "",
                                       "failure": {"code": "acp.timeout", "liveness": "silence"}})
    assert control.result["failure"]["liveness"] == "silence"


def test_role_worker_keeps_the_typed_cause_of_an_agent_stop():
    from garuda.acp.protocol import AcpCancelledError
    from garuda.runtime.role_worker import failure_of

    error = AcpTimeoutError("prompt exceeded its deadline")
    error.liveness = "deadline"
    assert failure_of(error, limited=True) == {
        "code": "acp.timeout", "detail": "prompt exceeded its deadline", "liveness": "deadline"}
    assert "liveness" not in failure_of(error, limited=False)
    cancelled = AcpCancelledError("agent ended the turn cancelled")
    cancelled.acp_stop_reason = "cancelled"
    assert failure_of(cancelled, limited=False) == {
        "code": "acp.cancelled", "detail": "agent ended the turn cancelled",
        "stop_reason": "cancelled"}


# --- the ACP adapter's silence watchdog -----------------------------------------------

def _adapter(profile, *extra, **kwargs):
    return AcpRuntime([*FAKE, "--profile", profile, *extra], runtime_id=f"fake-{profile}",
                      **kwargs)


async def test_a_silent_but_alive_harness_trips_the_watchdog_and_is_stopped():
    runtime = _adapter("silent")
    runtime.watch_silence(silence_sec=0.6, tool_silence_sec=30)
    await runtime.start(task="t", session_id=str(uuid.uuid4()))
    started = time.monotonic()
    with pytest.raises(AcpTimeoutError) as caught:
        await asyncio.wait_for(runtime.prompt("work"), 20)
    assert caught.value.liveness == "silence"
    assert time.monotonic() - started < 15  # the fake would sleep 60 s
    assert runtime.state.value == "failed"
    await runtime.close()


async def test_without_a_watchdog_a_silent_turn_is_not_stopped():
    """`garuda run`, the SDK and the ACP server ask for no watchdog: nothing changes."""
    runtime = _adapter("silent")
    await runtime.start(task="t", session_id=str(uuid.uuid4()))
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(runtime.prompt("work"), 2)
    await runtime.close()


async def test_a_long_tool_call_waits_for_the_longer_tool_silence():
    runtime = _adapter("long-tool", "--tool-seconds", "2")
    runtime.watch_silence(silence_sec=0.5, tool_silence_sec=20)
    await runtime.start(task="t", session_id=str(uuid.uuid4()))
    assert await asyncio.wait_for(runtime.prompt("work"), 20) == 1
    await runtime.close()


async def test_a_long_tool_call_still_stops_past_the_tool_silence():
    runtime = _adapter("long-tool", "--tool-seconds", "30")
    runtime.watch_silence(silence_sec=0.3, tool_silence_sec=0.8)
    await runtime.start(task="t", session_id=str(uuid.uuid4()))
    with pytest.raises(AcpTimeoutError) as caught:
        await asyncio.wait_for(runtime.prompt("work"), 20)
    assert caught.value.liveness == "silence"
    await runtime.close()


async def test_a_parked_approval_is_never_silence():
    async def slow_person(_action):
        await asyncio.sleep(2)
        return True

    runtime = _adapter("parked-approval", approval_handler=slow_person)
    runtime.watch_silence(silence_sec=0.5, tool_silence_sec=0.5)
    await runtime.start(task="t", session_id=str(uuid.uuid4()))
    assert await asyncio.wait_for(runtime.prompt("work"), 20) == 1
    await runtime.close()


# --- real ACP flows: stop, then `flow resume` -----------------------------------------

def _acp_runner(repo, limits, **kw):
    from garuda.flows.launch import launch_step

    config = {"version": 1, "roles": {"coder": {"harness": "fakeacp"}},
              "flows": {"one": {"steps": [{"id": "work", "role": "coder",
                                           "outputs": ["patch"]}]}}}
    resolved = gy.resolve(gy.parse(config), None)
    flow = json.loads(json.dumps(resolved.config["flows"]["one"]))
    flow["limits"] = limits
    return FlowRunner(SessionStore(), repo, "one", flow, resolved, task="t",
                      launcher=launch_step, **kw)


@pytest.mark.parametrize("profile, limits, code, error_code", [
    ("silent", {"silence_min": 0.01, "tool_silence_min": 1}, "flow.step_silent",
     "run.silence_exceeded"),
    ("silent", {"deadline_min": 0.02}, "flow.step_deadline", "run.deadline_exceeded"),
])
async def test_an_acp_step_stops_on_its_limit_and_resume_reruns_it(
        repo, tmp_path, monkeypatch, profile, limits, code, error_code):
    from tests.test_runtime_cli import _install_shim, _on_path, _pid_gone, _trusted_settings

    _on_path(monkeypatch, tmp_path / "bin")
    # The `silent` fake is silent on its first prompt only (counted in the state file).
    _install_shim(tmp_path / "bin", profile=profile, state_file=tmp_path / "fake-state.json")
    _trusted_settings(tmp_path, monkeypatch)

    started = time.monotonic()
    result = await asyncio.wait_for(_acp_runner(repo, limits).run(), 60)
    assert time.monotonic() - started < 30
    assert result.stopped is not None and result.stopped.code == code, result.stopped
    store = SessionStore()
    receipt, = engine.receipts(store, result.flow_session)
    assert receipt["status"] == "stopped" and receipt["stop"]["code"] == code
    assert receipt["stop"]["cause"]["code"] == error_code
    step = store.load_meta(receipt["session_id"])
    assert step["status"] == "failed" and step["error_code"] == error_code
    children = step.get("runtime_children", [])
    assert children and all(_pid_gone(c["pid"]) for c in children)
    assert LeaseStore().holders_of(repo) == []

    resumed = await asyncio.wait_for(
        _acp_runner(repo, limits, flow_session=result.flow_session).run(resume=True), 60)
    assert resumed.completed, resumed.stopped
    rows = engine.receipts(store, result.flow_session)
    assert [(r["attempt"], r["status"]) for r in rows] == [(1, "stopped"), (2, "done")]
    assert [e["event"] for e in engine.journal(store, result.flow_session)] == [
        "intent", "receipt", "intent", "receipt"]


async def test_an_acp_step_with_a_long_tool_call_completes_under_its_silence_limit(
        repo, tmp_path, monkeypatch):
    from tests.test_runtime_cli import _install_shim, _on_path, _trusted_settings

    _on_path(monkeypatch, tmp_path / "bin")
    _install_shim(tmp_path / "bin", profile="long-tool", extra=("--tool-seconds", "2"))
    _trusted_settings(tmp_path, monkeypatch)
    result = await asyncio.wait_for(
        _acp_runner(repo, {"silence_min": 0.01, "tool_silence_min": 1}).run(), 60)
    assert result.completed, result.stopped


# --- a checked (owned) step stops cleanly at its own deadline -------------------------

@pytest.fixture
async def case(tmp_path):
    """A parent flow lease on a fresh workspace, as in ``tests/test_owned_role.py``."""
    from garuda.interfaces.run_guard import WorkspaceLeaseGuard

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    subprocess.run(["git", "init", "-q", str(workspace)], check=True)
    leases = LeaseStore(tmp_path / "leases")
    parent = WorkspaceLeaseGuard(str(workspace), str(uuid.uuid4()), lease_store=leases, ttl_sec=1.2)
    parent.acquire()
    parent.start_heartbeat()
    try:
        yield workspace, parent, leases, SessionStore(tmp_path / "sessions")
    finally:
        await parent.release()


def _owned_acp(workspace, profile):
    import yaml

    from garuda.acp.fake_agent import MODE_AUTHORITY

    fixture = workspace.parent / "owned_acp.py"
    fixture.write_text(
        "import sys\n"
        f"sys.path.insert(0,{str(Path(__file__).resolve().parents[1])!r})\n"
        "from garuda.acp import fake_agent\n"
        f"raise SystemExit(fake_agent.main(['--profile',{profile!r}]))\n")
    Path(os.environ["GARUDA_GLOBAL_SETTINGS"]).write_text(yaml.safe_dump({"runtimes": [{
        "runtime_id": "owned-acp", "kind": "acp", "modes": MODE_AUTHORITY,
        "command": [sys.executable, str(fixture)], "version": "1"}],
        "capacity": {"owned-acp": 1}}))


@linux
async def test_an_owned_acp_step_stops_cleanly_at_its_deadline_without_quarantine(case):
    """Fails on the parent commit: the worker had no deadline, so only the supervisor's
    process timeout could end a long step, and that closure quarantines it."""
    from garuda.flows.engine import StepLaunch
    from garuda.runtime.capacity import CapacityStore
    from garuda.runtime.owned_role import execute_owned_role, prepare_owned_step
    from garuda.runtime.roles import RolePlan

    workspace, parent, _leases, store = case
    _owned_acp(workspace, "slow")
    role = RolePlan("builder", "owned-acp", "acp", permissions="auto")
    capability = parent.delegate(f"{parent.session_id}:build:1")
    launch = StepLaunch(parent.session_id, "build", "builder", role, "work slowly",
                        str(workspace), capability, 1, False, store,
                        limits=StepLimits(deadline_sec=2.0))
    request = prepare_owned_step(launch)
    assert request.timeout == 2.0 + 600
    assert json.loads(request.payload)["limits"] == {"deadline_sec": 2.0}
    started = time.monotonic()
    result = await asyncio.wait_for(execute_owned_role(request, capability), 60)
    assert time.monotonic() - started < 45  # the fake would sleep 60 s
    assert not result.success and result.liveness == "deadline"
    assert result.error_code == "acp.timeout" and "deadline" in result.error
    assert result.closure["process"]["outcome"] == "exited"
    assert result.closure["process"]["cleanup"] == {"backend": "linux-subreaper", "echild": True}
    assert not parent._quarantined
    assert store.load_meta(result.session_id)["error_code"] == "run.deadline_exceeded"
    CapacityStore().release(CapacityStore().reserve("owned-acp", "competitor", 1))


async def test_an_owned_step_without_limits_keeps_its_request_and_timeout(case):
    from garuda.flows.engine import StepLaunch
    from garuda.runtime.owned_role import prepare_owned_step
    from garuda.runtime.roles import RolePlan

    workspace, parent, _leases, store = case
    _owned_acp(workspace, "success")
    capability = parent.delegate(f"{parent.session_id}:build:1")
    launch = StepLaunch(parent.session_id, "build", "builder",
                        RolePlan("builder", "owned-acp", "acp", permissions="auto"), "t",
                        str(workspace), capability, 1, False, store)
    request = prepare_owned_step(launch)
    assert request.timeout == 3600 and "limits" not in json.loads(request.payload)
