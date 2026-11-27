"""Flow steps that run 10-12 hours: bounded event logs, lease heartbeats that ride out a
transient storage error, whole artifacts from owned steps, configurable check limits,
a visible 1 h backstop and approvals that say when their packet was partial."""

import argparse
import asyncio
import contextlib
import json
import os
import sys
import time
import uuid
from pathlib import Path

import pytest

from garuda.config import garuda_yaml as gy
from garuda.core.sessions import SessionStore

FAKE = str(Path(__file__).resolve().parents[1] / "garuda/acp/fake_agent.py")
linux = pytest.mark.skipif(sys.platform != "linux", reason="actual Linux role ownership")


# --- 1. the ACP event trail rotates by size and every reader reads all of it -------------


async def _two_acp_runs(store, workspace, sid):
    from garuda.acp.adapter import AcpRuntime

    store.begin(sid, task="fixture", model="external", agent="latest", workspace=str(workspace))
    polled = 0
    for index in range(2):
        runtime = AcpRuntime([sys.executable, FAKE, "--profile", "resume",
                              "--state-file", str(workspace.parent / f"wire-{index}.json")],
                             runtime_id="fakeacp", cwd=str(workspace), store=store,
                             persist_dir=str(store.session_dir(sid)))
        try:
            await runtime.start(task="fixture", session_id=sid)
            cursor = 0
            for turn in range(2):  # several turns, polled by cursor as the turn loop does
                await runtime.prompt(f"request {index}.{turn}")
                events, cursor = await runtime.poll_events(cursor)
                polled += len(events)
        finally:
            await runtime.close()
        polled += len((await runtime.poll_events(cursor))[0])
    return polled


def _all_rows(directory):
    from garuda.observability import event_log

    return [json.loads(line) for line in event_log.iter_lines(directory)]


def _readers(store, sid):
    from garuda.core import conversation
    from garuda.flows.trajectory import _events
    from garuda.interfaces.web.reads import external_trail
    from garuda.observability.lanes import read_cross_runtime

    directory = store.session_dir(sid)
    meta = store.load_meta(sid)
    return {"export": _events(store, sid),
            "lanes": read_cross_runtime(directory),
            "dashboard": external_trail(directory, meta, session_id=sid),
            "show": conversation.agent_info(store, sid, meta)}


async def test_a_rotated_trail_is_read_whole_by_every_reader(tmp_path, monkeypatch):
    from garuda.observability import event_log

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = SessionStore(tmp_path / "sessions")
    monkeypatch.setattr(event_log, "ROTATE_BYTES", 600)
    monkeypatch.setattr(event_log, "KEEP_ROTATED", 1000)
    polled = await _two_acp_runs(store, workspace, "rotated")

    directory = store.session_dir("rotated")
    parts = event_log.part_paths(directory)
    assert len(parts) > 2 and parts[-1].name == "acp-events.jsonl"
    assert parts[0].name == "acp-events.000001.jsonl"
    assert all(path.stat().st_size <= 600 or len(path.read_text().splitlines()) == 1
               for path in parts)
    rows = _all_rows(directory)
    kinds = [row["kind"] for row in rows]
    assert kinds.count("outbound_prompt") == 4
    assert polled == len(rows)  # rotation never touches the in-memory cursor the turn loop reads

    read = _readers(store, "rotated")
    events, summary = read["export"]
    assert summary["count"] == len(rows) and events == rows
    assert summary["parts"] == len(parts) and summary["pruned"] is False
    assert sum(read["lanes"].external_kinds.values()) == len(rows)
    assert read["dashboard"]["prompts_sent"] == 4
    executions = [s for s in read["show"]["segments"] if s["kind"] == "acp_execution"]
    assert len(executions) == 2 and all(s["prompts"] for s in executions)

    from garuda.interfaces.main import _save_external_trajectory

    out = tmp_path / "trajectory.jsonl"
    _save_external_trajectory(argparse.Namespace(_store=store, trajectory=str(out)), "rotated")
    assert [json.loads(line) for line in out.read_text().splitlines()] == rows


def test_rotation_keeps_n_parts_and_readers_say_earlier_ones_were_pruned(tmp_path):
    from garuda.core.failure_evidence import acp_trail_coverage
    from garuda.core.session_records import BoundedSessionRecords
    from garuda.flows.trajectory import _events
    from garuda.observability import event_log

    store = SessionStore(tmp_path / "sessions")
    store.begin("pruned", task="t", model="m", agent="a", workspace=str(tmp_path))
    directory = store.session_dir("pruned")
    for seq in range(40):
        event_log.append(directory, json.dumps({"kind": "message", "turn": 1, "seq": seq}) + "\n",
                         max_bytes=100, keep=2)
    names = sorted(path.name for path in directory.glob("acp-events*"))
    assert len(names) == 3 and "acp-events.000001.jsonl" not in names
    _rows, summary = _events(store, "pruned")
    assert summary["pruned"] is True and summary["parts"] == 3
    coverage = acp_trail_coverage(BoundedSessionRecords(store.root, byte_budget=1 << 20),
                                  "pruned", {"status": "present"})
    assert coverage["status"] == "clipped" and "rotated" in coverage["reason"]


def test_a_session_written_before_rotation_reads_as_before(tmp_path):
    from garuda.flows.trajectory import _events
    from garuda.interfaces.web.reads import external_trail
    from garuda.observability.lanes import read_cross_runtime

    store = SessionStore(tmp_path / "sessions")
    store.begin("legacy", task="t", model="m", agent="a", workspace=str(tmp_path))
    directory = store.session_dir("legacy")
    rows = [{"kind": "outbound_prompt", "turn": 1, "seq": 0},
            {"kind": "message", "turn": 1, "seq": 1}]
    (directory / "acp-events.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    events, summary = _events(store, "legacy")
    assert events == rows and summary["count"] == 2
    assert "parts" not in summary and summary["truncated"] is False
    assert read_cross_runtime(directory).external_kinds == {"outbound_prompt": 1, "message": 1}
    assert external_trail(directory, store.load_meta("legacy"))["prompts_sent"] == 1


# --- 2. a transient heartbeat failure is retried within the TTL ------------------------


def _guard(tmp_path, ttl=0.6):
    from garuda.interfaces.run_guard import WorkspaceLeaseGuard
    from garuda.workspace.lease import LeaseStore

    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    guard = WorkspaceLeaseGuard(str(workspace), str(uuid.uuid4()),
                                lease_store=LeaseStore(tmp_path / "leases"), ttl_sec=ttl)
    guard.acquire()
    return guard


def _failing_writes(guard, monkeypatch, failures):
    calls = []
    publish = guard.leases._publish_all

    def flaky(path, holders):
        calls.append(time.monotonic())
        if len(calls) <= failures:
            raise OSError(5, "Input/output error")
        return publish(path, holders)

    monkeypatch.setattr(guard.leases, "_publish_all", flaky)
    return calls


async def test_a_transient_heartbeat_failure_recovers_and_the_step_finishes(tmp_path, monkeypatch):
    guard = _guard(tmp_path)
    calls = _failing_writes(guard, monkeypatch, failures=2)
    guard.start_heartbeat()
    try:
        assert await guard.race(asyncio.sleep(1.2, result="done")) == "done"
        assert len(calls) >= 3  # two failed writes, then a renewal
    finally:
        await guard.release()


async def test_a_persistent_heartbeat_failure_cancels_before_the_ttl(tmp_path, monkeypatch):
    from garuda.interfaces.run_guard import HEARTBEAT_RETRIES
    from garuda.workspace.lease import LeaseError

    guard = _guard(tmp_path)
    calls = _failing_writes(guard, monkeypatch, failures=10**6)
    started = time.monotonic()
    guard.start_heartbeat()
    try:
        with pytest.raises(LeaseError, match="heartbeat failed"):
            await guard.race(asyncio.sleep(10))
        assert len(calls) == HEARTBEAT_RETRIES + 1
        assert calls[-1] - started < 0.6  # every attempt lay inside the TTL of the last write
    finally:
        await guard.release()


async def test_a_lost_lease_stops_at_once_without_retrying(tmp_path, monkeypatch):
    from garuda.workspace.lease import LeaseError

    guard = _guard(tmp_path)
    beats = []
    heartbeat = guard.leases.heartbeat

    def counted(*args, **kwargs):
        beats.append(time.monotonic())
        return heartbeat(*args, **kwargs)

    monkeypatch.setattr(guard.leases, "heartbeat", counted)
    for record in (tmp_path / "leases").glob("*.json"):
        record.unlink()  # expired and removed: this run no longer owns the workspace
    guard.start_heartbeat()
    try:
        with pytest.raises(LeaseError, match="no lease"):
            await guard.race(asyncio.sleep(10))
        assert len(beats) == 1
    finally:
        with contextlib.suppress(Exception):
            await guard.release()


def test_heartbeat_failures_are_classified():
    from garuda.interfaces.run_guard import transient_heartbeat_failure
    from garuda.runtime.strict_store import StorageError
    from garuda.workspace.lease import LeaseConflictError, LeaseError

    def caused(error, cause):
        try:
            raise error from cause
        except LeaseError as exc:
            return exc

    assert transient_heartbeat_failure(caused(LeaseError("lease storage unavailable"), OSError()))
    assert transient_heartbeat_failure(caused(LeaseError("unreadable lease"), StorageError()))
    assert not transient_heartbeat_failure(LeaseError("no lease for workspace w"))
    assert not transient_heartbeat_failure(caused(LeaseConflictError("held"), OSError()))


# --- 3. an owned step hands back a whole 60,000-character artifact ---------------------


@linux
async def test_a_60000_character_artifact_arrives_whole_through_an_owned_step(tmp_path):
    from garuda.flows import artifacts as art
    from garuda.interfaces.run_guard import WorkspaceLeaseGuard
    from garuda.runtime.process_supervision import LinuxProcessSupervisor, ProcessRequest
    from garuda.runtime.role_control import RoleControl
    from garuda.workspace.lease import LeaseStore

    workspace = str(tmp_path.resolve())
    parent = WorkspaceLeaseGuard(workspace, str(uuid.uuid4()), lease_store=LeaseStore(tmp_path / "leases"))
    parent.acquire()
    control = RoleControl(parent.delegate("worker"), workspace, "native")
    process = await LinuxProcessSupervisor().prepare()
    content = ("é" * 99 + "\n") * 600  # 60,000 characters, escaped 6 bytes each on the wire
    assert len(content) == 60_000 <= art.MAX_ARTIFACT_CHARS
    reply = "working notes\n" * 50 + f'<garuda-artifact type="patch">\n{content}\n</garuda-artifact>'
    script = '''
import json,os,sys
sys.path.insert(0,sys.argv[1])
from garuda.runtime.role_control import RoleControlClient, bounded_output
with os.fdopen(int(os.environ.pop('GARUDA_OWNED_INPUT_FD')),'r') as handle: payload=json.load(handle)
client=RoleControlClient(payload['control'])
client.acquire()
client.request('result',{'session_id':payload['session_id'],'success':True,
                         'output':bounded_output(payload['reply'])})
client.request('release')
client.close()
'''
    terminal = None
    try:
        endpoint = await control.start(process)
        await process.activate(ProcessRequest(
            (sys.executable, "-I", "-c", script, str(Path(__file__).resolve().parents[1])),
            workspace, timeout=20, purpose="role",
            input_data=json.dumps({"control": endpoint, "reply": reply,
                                   "session_id": str(uuid.uuid4())})))
        control.activated()
        terminal = await process.wait()
        assert terminal.exit_code == 0
        assert control.result is not None and control.result["success"] is True
        assert art.extract(control.result["output"]) == {"patch": content}
    finally:
        try:
            await process.close()
        finally:
            await control.close(closed=terminal is not None)
            await parent.release()


def test_only_the_result_may_pass_the_ordinary_control_bound():
    from garuda.runtime.role_control import (
        CONTROL_LIMIT,
        MAX_RESULT_OUTPUT,
        RESULT_REQUEST_LIMIT,
        bounded_output,
    )

    assert MAX_RESULT_OUTPUT > 4 * 64_000  # a whole artifact, even at 4 bytes a character
    worst = json.dumps({"output": "\x01" * MAX_RESULT_OUTPUT})
    assert len(worst) < RESULT_REQUEST_LIMIT and CONTROL_LIMIT == 65_536
    assert bounded_output("x" * 10) == "x" * 10


# --- 4. check limits are configurable up to explicit maxima ----------------------------


def test_check_limits_parse_within_their_maxima():
    doc = gy.parse({"version": 1, "check_limits": {"timeout_sec": 4 * 3600,
                                                   "phase_timeout_sec": 12 * 3600}})
    assert doc["check_limits"] == {"timeout_sec": 14_400, "phase_timeout_sec": 43_200}
    for key, value in (("timeout_sec", 4 * 3600 + 1), ("phase_timeout_sec", 12 * 3600 + 1),
                       ("timeout_sec", 0), ("timeout_sec", 1.5)):
        with pytest.raises(gy.GarudaConfigError) as exc:
            gy.parse({"version": 1, "check_limits": {key: value}})
        assert f"check_limits.{key}" in str(exc.value)
    with pytest.raises(gy.GarudaConfigError):
        gy.parse({"version": 1, "check_limits": {"retries": 2}})


def test_defaults_are_unchanged_the_user_raises_and_a_project_only_lowers():
    assert gy.check_limits(gy.resolve(None, None)) == {"timeout_sec": 900, "phase_timeout_sec": 3600}
    user = gy.parse({"version": 1, "check_limits": {"timeout_sec": 7200, "phase_timeout_sec": 36000}})
    lower = gy.parse({"version": 1, "check_limits": {"timeout_sec": 1800}})
    resolved = gy.resolve(user, lower)
    assert gy.check_limits(resolved) == {"timeout_sec": 1800, "phase_timeout_sec": 36000}
    assert resolved.provenance["check_limits.timeout_sec"] == gy.PROJECT
    assert gy.check_limits(gy.resolve(None, gy.parse(
        {"version": 1, "check_limits": {"phase_timeout_sec": 600}})))["phase_timeout_sec"] == 600
    with pytest.raises(gy.GarudaConfigError) as exc:
        gy.resolve(None, lower)  # 1800 would raise the 900 s default
    assert exc.value.code == "config.project_widening"


def test_configured_limits_reach_the_frozen_request_and_its_records():
    from garuda.core.acceptance_records import CheckRecords
    from garuda.core.acceptance_request import CheckLimits
    from garuda.runtime.process_request import ProcessRequest

    longest = CheckLimits(timeout=4 * 3600, phase_timeout=12 * 3600).document()
    CheckRecords._limits(longest)
    assert ProcessRequest("true", "/", timeout=4 * 3600).document()["timeout"] == 14_400
    for limits in (CheckLimits(timeout=4 * 3600 + 1), CheckLimits(phase_timeout=12 * 3600 + 1)):
        with pytest.raises(ValueError):
            limits.document()


def test_freezing_checks_uses_the_configured_limits(monkeypatch):
    from garuda.core import acceptance_request

    captured = {}

    def freeze(checks, sources, permissions, *, limits=None):
        captured["limits"] = limits

    monkeypatch.setattr(acceptance_request.FrozenCheckRequest, "freeze", staticmethod(freeze))
    monkeypatch.setattr("garuda.core.acceptance.checks_with_authority", lambda resolved: [])
    resolved = gy.resolve(gy.parse({"version": 1, "check_limits": {"timeout_sec": 3 * 3600}}), None)
    acceptance_request.freeze_resolved_checks(resolved, "/", [], None)
    assert captured["limits"].timeout == 3 * 3600 and captured["limits"].phase_timeout == 3600


# --- 5. a checked step past the 1 h backstop says why it was quarantined ---------------


def test_a_long_deadline_fits_the_owned_supervisor():
    from garuda.flows.engine import StepLimits
    from garuda.runtime.owned_role import owned_timeout
    from garuda.runtime.process_request import ProcessRequest

    for minutes in (720, 7 * 24 * 60):  # a 12 h step, and the longest deadline_min
        timeout = owned_timeout(StepLimits(minutes * 60.0))
        ProcessRequest(("true",), "/", timeout=timeout, purpose="role").document()


async def test_the_backstop_stop_is_recorded_and_shown(tmp_path, monkeypatch, capsys):
    from garuda.runtime.owned_role import StepBackstopExceeded
    from garuda.runtime.process_supervision import CleanupUncertain
    from tests.test_flows import CONFIG, Fake, _git, _plan, _runner
    from tests.test_runtime_cli import _main

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "a.txt").write_text("a\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "base")

    async def killed(launch):
        raise StepBackstopExceeded("build", 3600, None)

    runner = _runner(repo, Fake(repo, {"plan": _plan, "build": killed}))
    with pytest.raises(CleanupUncertain):
        await runner.run()
    meta = SessionStore().load_meta(runner.flow_session)
    assert meta["flow_state"] == "quarantined"  # the quarantine itself is unchanged
    assert meta["flow_stop"]["code"] == "flow.step_backstop"
    assert meta["flow_stop"]["step"] == "build"
    assert "limits.deadline_min" in meta["flow_stop"]["message"]
    gy.user_path().write_text(gy.dump(CONFIG))
    code, out = _main(monkeypatch, capsys, "flow", "show", runner.flow_session,
                      "--workspace", str(repo))
    assert code == 0, out
    assert out.splitlines()[0] == "flow pbr: quarantined"
    assert "stopped at step build: flow.step_backstop:" in out
    assert "3600 s backstop" in out and "must set limits.deadline_min" in out


@linux
async def test_an_owned_step_killed_by_the_backstop_raises_the_typed_stop(tmp_path, monkeypatch):
    import subprocess

    from garuda.agents import role_agent
    from garuda.flows.engine import StepLaunch
    from garuda.interfaces.run_guard import WorkspaceLeaseGuard
    from garuda.runtime.owned_role import StepBackstopExceeded, execute_owned_role, prepare_owned_step
    from garuda.runtime.roles import RolePlan
    from garuda.workspace.lease import LeaseStore
    from tests.test_owned_role import install_native

    monkeypatch.setenv("OPENAI_API_KEY", "local-protocol-fixture")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    subprocess.run(["git", "init", "-q", str(workspace)], check=True)
    parent = WorkspaceLeaseGuard(str(workspace), str(uuid.uuid4()),
                                 lease_store=LeaseStore(tmp_path / "leases"), ttl_sec=1.2)
    parent.acquire()
    parent.start_heartbeat()
    store = SessionStore(tmp_path / "sessions")
    server, _requests = install_native(workspace, Path(os.environ["GARUDA_GLOBAL_SETTINGS"]))
    try:
        role = role_agent.bind(RolePlan("builder", "native", "native", permissions="auto",
                                        profile="owned"), str(workspace))
        capability = parent.delegate(f"{parent.session_id}:build:1")
        request = prepare_owned_step(StepLaunch(parent.session_id, "build", "builder", role, "run long",
                                                str(workspace), capability, 1, False, store), timeout=3)
        with pytest.raises(StepBackstopExceeded) as stopped:
            await asyncio.wait_for(execute_owned_role(request, capability), 30)
        assert stopped.value.stop["code"] == "flow.step_backstop"
        assert "limits.deadline_min" in stopped.value.stop["message"]
        assert parent._quarantined  # retained exactly as any unproved close
    finally:
        (workspace / "release").touch()
        server.shutdown()
        server.server_close()
        await parent.release()


# --- 6. an approval over a partial packet says so --------------------------------------


@pytest.mark.parametrize("files, coverage", [(451, "partial"), (3, "complete")])
async def test_an_approval_records_and_shows_its_packet_coverage(tmp_path, monkeypatch, capsys,
                                                                 files, coverage):
    from garuda.interfaces.main import _review_approval_lines
    from tests.test_flow_review import APPROVE, ManyFilesScript, _config, _run
    from tests.test_flows import _git

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "a.txt").write_text("a\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "base")
    result = await _run(repo, ManyFilesScript([APPROVE], files), _config(max_rounds=0)).run()
    assert result.completed
    meta = SessionStore().load_meta(result.flow_session)
    assert meta["review_approvals"]["build"]["coverage"] == coverage
    assert meta["review_packet"]["changed_complete"] is (coverage == "complete")
    (line,) = _review_approval_lines(meta)
    assert line.startswith(f"  review of build: approved; packet coverage: {coverage}")
    if coverage == "partial":
        assert "did not cover every changed file" in line


def test_packet_coverage_reads_both_bounds():
    from garuda.flows.review import packet_coverage

    assert packet_coverage({"changed": {"complete": True}, "diff": {"complete": True}}) == "complete"
    assert packet_coverage({"changed": {"complete": True}, "diff": {"complete": False}}) == "partial"
    assert packet_coverage({"changed": {"complete": False}, "diff": {"complete": None}}) == "partial"
    assert packet_coverage({"changed": {"complete": None}, "diff": {"complete": True}}) == "unknown"
