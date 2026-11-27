"""#760: a checked flow killed mid-step settles, or is recovered, without a dead end.

The flow parent is a real `garuda flow run --check` subprocess whose step runs
the fake ACP agent (`slow`: it never answers its prompt). SIGTERM must cancel
the step like Ctrl-C: the owned worker tree is reaped, the attempt is recorded
as a settled `flow.interrupted` stop with its closure and evidence, the lease
and slot are released and `flow resume` reruns the step under the original
check. SIGKILL leaves the flow `running` with a dead owner's lease and slot:
`garuda runtime recover --session <flow>` releases them only once the owner,
every step worker, every process carrying its lineage and every recorded
owned container are proved gone, and `flow resume` then continues it with the
next attempt (#856).
"""

from __future__ import annotations

import json
import os
import shlex
import signal
import stat
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

from garuda.config import garuda_yaml as gy
from garuda.core.sessions import SessionStore
from garuda.flows import engine
from tests.test_roles import REPO, _repo

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="owned flow checks require Linux")

FLOW = ("version: 1\nroles:\n  slowpoke: {harness: fakeslow}\n"
        "flows:\n  one:\n    steps:\n    - {id: only, role: slowpoke}\n")


def _shim(path, profile, marker):
    path.write_text(f"#!/bin/sh\ntouch {shlex.quote(str(marker))}\n"
                    f"PYTHONPATH={shlex.quote(str(REPO))} exec "
                    f"{shlex.quote(sys.executable)} -m garuda.acp.fake_agent "
                    f"--profile {profile} --agent-info '{{\"version\": \"1\"}}' \"$@\"\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


@pytest.fixture
def flow_owner(tmp_path, monkeypatch):
    """Start `garuda flow run one --check true` as a real subprocess on a slow agent."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    marker = tmp_path / "agent-started"
    shim = bin_dir / "slow-shim"
    _shim(shim, "slow", marker)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    settings = Path(os.environ["GARUDA_GLOBAL_SETTINGS"])  # per test (conftest)
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(
        "runtimes:\n"
        "  - {runtime_id: fakeslow, kind: acp, command: [slow-shim], version: '1', setup: shim,\n"
        "     modes: {ask: readonly, edit: smart, judge: auto, everything: yolo}}\n"
        "capacity: {fakeslow: 2}\n")
    gy.user_path().write_text(FLOW)
    workspace = _repo(tmp_path / "ws")
    started = []

    def start():
        proc = subprocess.Popen(
            [sys.executable, "-c",
             "import sys; sys.argv = ['garuda', *sys.argv[1:]]; "
             "from garuda.interfaces.main import main; main()",
             "flow", "run", "one", "-t", "say hi", "--workspace", str(workspace),
             "--check", "true"],
            cwd=str(workspace), env=dict(os.environ, PYTHONPATH=str(REPO)),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            start_new_session=True)
        started.append(proc)
        deadline = time.monotonic() + 60
        while not marker.exists():
            assert proc.poll() is None, proc.stdout.read()
            assert time.monotonic() < deadline, "the step agent never started"
            time.sleep(0.1)
        time.sleep(1.5)  # the prompt is now in flight
        flow_id = next(m["session_id"] for m in SessionStore().list_sessions(limit=None)
                       if m.get("kind") == "flow")
        return proc, flow_id

    yield start, workspace, shim, marker
    for proc in started:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        proc.wait(timeout=30)


def _cli(workspace, *argv):
    """`garuda ARGV` in a fresh process in the workspace, as a user's next command runs."""
    done = subprocess.run(
        [sys.executable, "-c", "import sys; sys.argv = ['garuda', *sys.argv[1:]]; "
         "from garuda.interfaces.main import main; main()", *argv],
        cwd=str(workspace), env=dict(os.environ, PYTHONPATH=str(REPO)),
        capture_output=True, text=True, timeout=180)
    return done.returncode, done.stdout + done.stderr


def _main(monkeypatch, capsys, *argv):
    from tests.test_runtime_cli import _main as main

    return main(monkeypatch, capsys, *argv)


def _holdings_free(workspace, runtime="fakeslow"):
    import asyncio

    from garuda.interfaces.run_guard import WorkspaceLeaseGuard
    from garuda.runtime.capacity import CapacityStore

    slots = CapacityStore()
    competitor = WorkspaceLeaseGuard(str(workspace), str(uuid.uuid4()))
    competitor.acquire()
    asyncio.run(competitor.release())
    assert not [s for s in _all_slots(slots) if s["key"] == runtime], _all_slots(slots)


def _all_slots(slots):
    out = []
    if not slots.root.is_dir():
        return out
    for path in slots.root.glob("*.json"):
        document = json.loads(path.read_text())
        out += [{"key": document["key"], "holder": holder} for holder in document.get("slots", {})]
    return out


def _wait_gone(pid, timeout=30.0):
    from garuda.runtime.ownership import owner_liveness

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        with open(f"/proc/{pid}/stat") as handle:
            if handle.read().rsplit(")", 1)[1].split()[0] in {"Z", "X"}:
                return
        time.sleep(0.1)
    raise AssertionError(f"pid {pid} still alive ({owner_liveness({'pid': pid})})")


def test_sigterm_settles_a_checked_flow_as_a_resumable_interrupted_stop(
        flow_owner, monkeypatch, capsys):
    from garuda.runtime.ownership import surviving_descendants

    start, workspace, shim, marker = flow_owner
    proc, flow_id = start()
    from garuda.runtime.capacity import CapacityStore

    assert any(s["holder"] == f"{flow_id}:only:1" for s in _all_slots(CapacityStore()))

    proc.send_signal(signal.SIGTERM)  # only the flow process, as `timeout` or systemd would
    out = proc.communicate(timeout=120)[0]

    assert proc.returncode == 143, out
    stopped = json.loads(next(line for line in out.splitlines() if line.startswith("{")))
    assert stopped == {"flow_session": flow_id, "state": "stopped", "code": "flow.interrupted"}
    store = SessionStore()
    meta = store.load_meta(flow_id)
    assert meta["flow_state"] == "stopped" and meta["flow_stop"]["code"] == "flow.interrupted"
    assert isinstance(meta["flow_resume_checkpoint"], dict)
    (receipt,) = engine.receipts(store, flow_id)
    assert receipt["status"] == "stopped" and receipt["stop"]["code"] == "flow.interrupted"
    assert receipt["closure"]["process"]["outcome"] == "cancelled"
    assert receipt["closure"]["process"]["cleanup"] == {"backend": "linux-subreaper", "echild": True}
    assert receipt["evidence_boundary"]["boundary_digest"]
    step = store.load_meta(receipt["session_id"])
    assert step["status"] == "cancelled" and step["error_code"] == "flow.interrupted"
    # Nothing the flow launched survives, and nothing is held.
    assert surviving_descendants(receipt["closure"]["parent_owner"]) == []
    _holdings_free(workspace)

    # The settled stop resumes under the original check.
    _shim(shim, "success", marker)
    rc, resumed = _cli(workspace, "flow", "resume", flow_id)
    assert rc == 0, resumed
    meta = store.load_meta(flow_id)
    assert meta["flow_state"] == "completed"
    assert meta["state"]["verification"] == {"status": "passed", "authority": "user-request"}
    assert [(r["step"], r["attempt"], r["status"]) for r in engine.receipts(store, flow_id)] == [
        ("only", 1, "stopped"), ("only", 2, "done")]
    _holdings_free(workspace)


def test_sigkill_holdings_are_recovered_only_when_everything_is_gone(
        flow_owner, monkeypatch, capsys):
    from garuda.runtime import recovery
    from garuda.runtime.capacity import CapacityStore
    from garuda.runtime.ownership import LINEAGE_ENV, lineage_token
    from garuda.workspace.lease import LeaseConflictError, LeaseStore

    start, workspace, _shim_path, _marker = flow_owner
    proc, flow_id = start()
    store = SessionStore()
    (holder,) = LeaseStore().live_holders_for_session(flow_id)
    step_id = next(m["session_id"] for m in store.list_sessions(limit=None)
                   if (m.get("flow_step") or {}).get("flow_session") == flow_id)
    worker = store.load_meta(step_id)["state"]["owner"]["pid"]

    proc.kill()
    proc.wait(timeout=30)
    _wait_gone(worker)  # the step supervisor reaps its tree when its parent dies
    meta = store.load_meta(flow_id)
    assert meta["flow_state"] == "running"
    assert any(s["holder"] == f"{flow_id}:only:1" for s in _all_slots(CapacityStore()))

    # A step session points at its flow instead of a native/ACP refusal.
    rc, out = _main(monkeypatch, capsys, "runtime", "recover", "--session", step_id)
    assert rc != 0 and f"garuda runtime recover --session {flow_id}" in out, out

    # An escaped descendant still carries the dead owner's lineage, and a
    # confined step recorded a container whose removal was never proved.
    survivor = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)"], start_new_session=True,
        env=dict(os.environ, **{LINEAGE_ENV: lineage_token(holder.pid, holder.identity)}))
    record = store.root / "owned-docker" / "step-container"
    record.mkdir(parents=True)
    names = {"probe": "garuda-role-x-probe", "run": "garuda-role-x-run"}
    (record / "intent.json").write_text(json.dumps(
        {"version": 1, "session_id": "x", "parent_id": flow_id, "names": names}))
    present = {"answer": True}
    monkeypatch.setattr(recovery, "docker_container_exists", lambda name: present["answer"])
    try:
        rc, out = _main(monkeypatch, capsys, "runtime", "recover", "--session", flow_id)
        assert rc != 0 and "docker rm -f garuda-role-x-probe" in out, out
        present["answer"] = None
        rc, out = _main(monkeypatch, capsys, "runtime", "recover", "--session", flow_id)
        assert rc != 0 and "Docker cannot confirm" in out, out
        present["answer"] = False
        rc, out = _main(monkeypatch, capsys, "runtime", "recover", "--session", flow_id)
        assert rc != 0 and f"descendant process(es) {survivor.pid}" in out, out
        # Every refusal kept everything.
        with pytest.raises(LeaseConflictError):
            from garuda.interfaces.run_guard import WorkspaceLeaseGuard

            WorkspaceLeaseGuard(str(workspace), str(uuid.uuid4())).acquire()
        assert store.load_meta(flow_id)["flow_state"] == "running"
    finally:
        survivor.kill()
        survivor.wait(timeout=10)

    rc, out = _main(monkeypatch, capsys, "runtime", "recover", "--session", flow_id)
    assert rc == 0, out
    assert f"session {flow_id}: flow" in out and "released mutating workspace lease" in out, out
    assert f"released fakeslow capacity slot {flow_id}:only:1" in out, out
    _holdings_free(workspace)
    meta = store.load_meta(flow_id)
    assert meta["flow_state"] == "interrupted" and meta["flow_stop"]["code"] == "flow.owner_lost"
    assert meta["flow_stop"]["step"] == "only"
    assert store.load_meta(step_id)["status"] == "failed"

    # The killed attempt's tree is proved gone (its owned worker ran under the
    # subreaper), so `flow resume` settles it and runs the step again (#856).
    _shim(_shim_path, "success", _marker)
    rc, out = _cli(workspace, "flow", "resume", flow_id)
    assert rc == 0, out
    assert [(r["step"], r["attempt"], r["status"], (r.get("stop") or {}).get("code"))
            for r in engine.receipts(store, flow_id)] == [
        ("only", 1, "stopped", "flow.interrupted"), ("only", 2, "done", None)]


def test_a_live_flow_owner_is_never_recovered(flow_owner, monkeypatch, capsys):
    start, workspace, _shim_path, _marker = flow_owner
    proc, flow_id = start()
    try:
        rc, out = _main(monkeypatch, capsys, "runtime", "recover", "--session", flow_id)
        assert rc != 0 and "still running" in out, out
        rc, out = _main(monkeypatch, capsys, "flow", "resume", flow_id)
        assert rc == 2 and "flow.recovery_required" in out, out
        assert SessionStore().load_meta(flow_id)["flow_state"] == "running"
    finally:
        proc.send_signal(signal.SIGTERM)
        proc.communicate(timeout=120)
    assert proc.returncode == 143
