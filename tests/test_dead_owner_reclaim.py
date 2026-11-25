"""A killed run's lease and capacity slot can be reclaimed, but only from a proved-dead owner (#676, #703).

Every owner here is a real subprocess that takes ownership through the
ordinary stores and is then killed with SIGKILL, as `kill -9` does to a run.
"""

import json
import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from garuda.runtime.capacity import CapacityError, CapacityStore
from garuda.runtime.ownership import LINEAGE_ENV, lineage_token, owner_liveness, surviving_descendants
from garuda.workspace.lease import LeaseConflictError, LeaseStore

ROOT = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.skipif(not os.path.isfile("/proc/self/environ"),
                                reason="descendant lineage scan reads /proc")

OWNER = '''
    import json, subprocess, sys, time
    from garuda.runtime.capacity import CapacityStore
    from garuda.workspace.lease import LeaseStore
    leases_root, capacity_root, workspace, session, mode, THREAD_LEADER = sys.argv[1:7]
    leases = LeaseStore(leases_root)
    leases.acquire(workspace, session, ttl_sec=1)
    capacity = CapacityStore(capacity_root)
    held = capacity.reserve("claude", session + ":fix:1", 2)
    if mode == "quarantine":
        leases.quarantine(workspace, session)
        capacity.quarantine(held)
    orphan = None
    if mode == "orphan":
        orphan = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"],
                                  stdin=subprocess.DEVNULL, start_new_session=True).pid
    if mode == "thread-leader":
        orphan = subprocess.Popen([sys.executable, "-c", THREAD_LEADER],
                                  stdin=subprocess.DEVNULL, start_new_session=True).pid
    print(json.dumps({"orphan": orphan}), flush=True)
    time.sleep(120)
'''


# A process whose main thread has exited while another thread still runs: its
# leader shows as a zombie, yet the process can still write.
THREAD_LEADER = (
    "import ctypes, threading, time\n"
    "threading.Thread(target=lambda: time.sleep(120)).start()\n"
    "ctypes.CDLL(None).pthread_exit(None)\n"
)

# Starts an unrelated daemon (no lineage, orphaned by a double fork) that keeps
# one unreaped zombie child, then prints the daemon's and the zombie's pids.
ZOMBIE_KEEPER = """
import os, sys, time
if os.fork():
    os._exit(0)
os.setsid()
zombie = os.fork()
if zombie == 0:
    os._exit(0)
print(os.getpid(), zombie, flush=True)
time.sleep(120)
"""


def _state(pid):
    text = Path(f"/proc/{pid}/stat").read_text()
    return text[text.rfind(")") + 2:].split()[0]


def _wait_state(pid, state, timeout=10):
    deadline = time.monotonic() + timeout
    while _state(pid) != state:
        assert time.monotonic() < deadline, f"pid {pid} never reached state {state}"
        time.sleep(0.02)

def _wait_dead(pid, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.02)
    raise AssertionError(f"pid {pid} did not die")


@pytest.fixture
def owner(tmp_path):
    """Start a real owner subprocess; yields a starter returning (proc, info)."""
    started = []

    def start(mode="plain", session="crashed"):
        workspace = tmp_path / "workspace"
        workspace.mkdir(exist_ok=True)
        proc = subprocess.Popen(
            [sys.executable, "-c", textwrap.dedent(OWNER), str(tmp_path / "leases"),
             str(tmp_path / "capacity"), str(workspace), session, mode, THREAD_LEADER],
            env=dict(os.environ, PYTHONPATH=str(ROOT)), stdout=subprocess.PIPE,
            text=True, start_new_session=True)
        started.append(proc)
        line = proc.stdout.readline()
        assert line, "owner failed to start"
        info = json.loads(line)
        if info["orphan"]:
            started.append(info["orphan"])
        return proc, info, workspace

    yield start
    for item in started:
        pid = item if isinstance(item, int) else item.pid
        try:
            os.killpg(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        if not isinstance(item, int):
            item.wait(timeout=10)


def _kill(proc):
    os.killpg(proc.pid, signal.SIGKILL)
    proc.wait(timeout=10)


def _stores(tmp_path):
    return LeaseStore(tmp_path / "leases"), CapacityStore(tmp_path / "capacity")


def test_a_live_owner_is_never_reclaimed_and_a_killed_one_is(owner, tmp_path):
    proc, _info, workspace = owner()
    leases, capacity = _stores(tmp_path)
    holder, = leases.holders_of(workspace)
    slot, = capacity.slots_for_session("crashed")
    time.sleep(1.1)  # the lease's heartbeat has expired, as after any crash
    with pytest.raises(LeaseConflictError, match="still running"):
        leases.reclaim_dead(holder)
    with pytest.raises(CapacityError, match="still running"):
        capacity.reclaim_dead(slot["key"], slot["holder"], slot["owner"])
    assert leases.holders_of(workspace) == [holder]
    assert capacity.holders("claude") == ["crashed:fix:1"]

    _kill(proc)
    with pytest.raises(LeaseConflictError, match="garuda runtime recover --session crashed"):
        leases.acquire(workspace, "next")
    assert leases.reclaim_dead(holder) is True
    assert capacity.reclaim_dead(slot["key"], slot["holder"], slot["owner"]) is True
    assert leases.holders_of(workspace) == []
    assert capacity.holders("claude") == []
    assert leases.reclaim_dead(holder) is False  # already gone: nothing to do
    leases.acquire(workspace, "next")  # the workspace is usable again


def test_a_surviving_descendant_of_a_killed_owner_keeps_both(owner, tmp_path):
    proc, info, workspace = owner("orphan")
    leases, capacity = _stores(tmp_path)
    holder, = leases.holders_of(workspace)
    slot, = capacity.slots_for_session("crashed")
    _kill(proc)
    owner_record = {"pid": holder.pid, "identity": holder.identity, "pgid": holder.pgid}
    assert owner_liveness(owner_record) is False  # the parent alone looks dead
    assert surviving_descendants(owner_record) == [info["orphan"]]
    with pytest.raises(LeaseConflictError, match=f"descendant process.*{info['orphan']}"):
        leases.reclaim_dead(holder)
    with pytest.raises(CapacityError, match=str(info["orphan"])):
        capacity.reclaim_dead(slot["key"], slot["holder"], slot["owner"])
    assert leases.holders_of(workspace) == [holder]

    os.killpg(info["orphan"], signal.SIGKILL)
    _wait_dead(info["orphan"])
    assert leases.reclaim_dead(holder) is True
    assert capacity.reclaim_dead(slot["key"], slot["holder"], slot["owner"]) is True


def test_an_unrelated_zombie_does_not_block_reclaim(owner, tmp_path):
    """A zombie runs no code: its unreadable environ is not an inconclusive scan (#731)."""
    proc, _info, workspace = owner()
    leases, capacity = _stores(tmp_path)
    holder, = leases.holders_of(workspace)
    slot, = capacity.slots_for_session("crashed")
    _kill(proc)
    env = {k: v for k, v in os.environ.items() if k != LINEAGE_ENV}
    keeper = subprocess.Popen([sys.executable, "-c", ZOMBIE_KEEPER], env=env,
                              stdout=subprocess.PIPE, text=True)
    daemon, zombie = map(int, keeper.stdout.readline().split())
    keeper.wait(timeout=10)
    try:
        _wait_state(zombie, "Z")
        owner_record = {"pid": holder.pid, "identity": holder.identity, "pgid": holder.pgid}
        assert surviving_descendants(owner_record) == []
        assert leases.reclaim_dead(holder) is True
        assert capacity.reclaim_dead(slot["key"], slot["holder"], slot["owner"]) is True
    finally:
        os.killpg(daemon, signal.SIGKILL)


def test_a_zombie_leader_with_a_running_thread_still_blocks_reclaim(owner, tmp_path):
    """A process whose main thread exited still runs; it is never skipped as exited."""
    from garuda.runtime.ownership import _exited
    from garuda.runtime.recovery import _linux_stat

    proc, info, workspace = owner("thread-leader")
    leases, _capacity = _stores(tmp_path)
    holder, = leases.holders_of(workspace)
    _wait_state(info["orphan"], "Z")
    _kill(proc)
    assert _exited(info["orphan"], _linux_stat) is False
    with pytest.raises(LeaseConflictError, match=f"(cannot be inspected|descendant).*{info['orphan']}"):
        leases.reclaim_dead(holder)
    assert leases.holders_of(workspace) == [holder]

def test_a_quarantined_holding_is_never_reclaimed_after_its_owner_dies(owner, tmp_path):
    proc, _info, workspace = owner("quarantine")
    leases, capacity = _stores(tmp_path)
    holder, = leases.holders_of(workspace)
    slot, = capacity.slots_for_session("crashed")
    assert holder.quarantined and slot["quarantined"] is True
    _kill(proc)
    with pytest.raises(LeaseConflictError, match="quarantined"):
        leases.reclaim_dead(holder)
    with pytest.raises(CapacityError, match="quarantined"):
        capacity.reclaim_dead(slot["key"], slot["holder"], slot["owner"])
    time.sleep(1.1)  # past the lease TTL, as after any crash
    with pytest.raises(LeaseConflictError, match="quarantined by its owner"):
        leases.acquire(workspace, "next")


def test_lineage_is_inherited_by_children_launched_after_ownership(tmp_path):
    from garuda.acp.client import child_environment
    from garuda.runtime.ownership import current_owner
    from garuda.runtime.recovery import _process_identity

    me = current_owner()
    token = lineage_token(me.pid, me.identity)
    assert token in os.environ[LINEAGE_ENV].split()
    # ACP children get a minimal environment, which still carries the lineage.
    assert token in child_environment()[LINEAGE_ENV].split()
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        assert child.pid in surviving_descendants(
            {"pid": me.pid, "identity": _process_identity(me.pid)})
    finally:
        child.kill()
        child.wait()


def test_an_unreadable_process_is_ruled_out_only_through_an_older_ancestor():
    from garuda.runtime.ownership import _older_lineage

    def fields(ppid, start):
        return ["comm", "S", str(ppid), *["0"] * 17, str(start)]

    table = {50: fields(40, 900), 40: fields(1, 100),  # under an older live process
             60: fields(1, 900),                        # orphaned to init
             70: fields(71, 900), 71: fields(1, 800),   # its whole chain is younger
             80: fields(81, 900)}                       # ancestor vanished
    assert _older_lineage(50, 500, table.get) is True
    assert _older_lineage(40, 500, table.get) is True
    assert _older_lineage(60, 500, table.get) is False
    assert _older_lineage(70, 500, table.get) is False
    assert _older_lineage(80, 500, table.get) is False


def _session(tmp_path, session_id="crashed"):
    from garuda.core.sessions import SessionStore

    sessions = SessionStore(tmp_path / "sessions")
    sessions.begin(session_id, task="t", model="m", agent="a", workspace=str(tmp_path))
    sessions.checkpoint_messages(session_id, [])
    return sessions, sessions.ensure_unified(session_id)


def _dead_child_record(sessions, session_id, runtime_id, owner_pid, owner_identity):
    from garuda.runtime.recovery import _process_identity

    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                             start_new_session=True)
    identity = _process_identity(child.pid)
    child.kill()
    child.wait()
    sessions.update_meta(session_id, {"runtime_children": [{
        "session_id": session_id, "runtime_id": runtime_id, "pid": child.pid,
        "process_group": child.pid, "identity": identity,
        "owner": {"pid": owner_pid, "identity": owner_identity}, "state": "live"}]})
    return child.pid


def test_runtime_recover_releases_a_killed_runs_lease_and_step_slot(owner, tmp_path):
    from garuda.interfaces.runtime_cli import cmd_recover
    from garuda.runtime.recovery import RecoveryError, recover

    sessions, unified = _session(tmp_path)
    proc, _info, workspace = owner()
    leases, capacity = _stores(tmp_path)
    holder, = leases.holders_of(workspace)
    # The crashed adapter is still recorded `live` although its pid is gone.
    child_pid = _dead_child_record(sessions, "crashed", unified.active.runtime_id,
                                   holder.pid, holder.identity)
    time.sleep(1.1)
    with pytest.raises(RecoveryError, match="still running"):
        recover(sessions, "crashed", leases=leases, capacity=capacity, release_dead_owner=True)
    _kill(proc)
    # Ordinary recovery (resume paths) still refuses on any retained lease.
    with pytest.raises(RecoveryError, match="retained mutating workspace lease"):
        recover(sessions, "crashed", leases=leases)

    report = recover(sessions, "crashed", leases=leases, capacity=capacity,
                     release_dead_owner=True)
    assert any("released mutating workspace lease" in note for note in report.notes)
    assert any("released claude capacity slot crashed:fix:1" in note for note in report.notes)
    assert leases.holders_of(workspace) == []
    assert capacity.all_slots() == []
    child, = sessions.load_meta("crashed")["runtime_children"]
    assert (child["pid"], child["state"]) == (child_pid, "exited")
    # Default stores: the CLI command now succeeds rather than always refusing.
    assert "session crashed:" in cmd_recover(sessions, "crashed")


def test_runtime_recover_keeps_everything_while_a_descendant_survives(owner, tmp_path):
    from garuda.runtime.recovery import RecoveryError, recover

    sessions, _unified = _session(tmp_path)
    proc, info, workspace = owner("orphan")
    leases, capacity = _stores(tmp_path)
    _kill(proc)
    with pytest.raises(RecoveryError, match=f"{info['orphan']}"):
        recover(sessions, "crashed", leases=leases, capacity=capacity, release_dead_owner=True)
    assert len(leases.holders_of(workspace)) == 1
    assert capacity.holders("claude") == ["crashed:fix:1"]


def test_doctor_and_ownership_view_report_dead_owner_holdings(owner, tmp_path, monkeypatch):
    from garuda.core.ownership_read_model import _capacity, _leases
    from garuda.interfaces.onboarding import _state_diagnostics
    from garuda.runtime import capacity as capacity_module

    monkeypatch.setenv("GARUDA_LEASES_DIR", str(tmp_path / "leases"))
    monkeypatch.setattr(capacity_module, "default_capacity_root", lambda: tmp_path / "capacity")
    proc, _info, _workspace = owner()
    time.sleep(1.1)
    codes = {d.code for d in _state_diagnostics()}
    assert {"lease.overdue", "capacity.held"} <= codes and "lease.stale" not in codes
    _kill(proc)
    stale = {d.code: d for d in _state_diagnostics()}
    assert "garuda runtime recover --session crashed" in stale["lease.stale"].fix
    assert "garuda runtime recover --session crashed" in stale["capacity.stale"].fix
    # Step-keyed slots are listed for their flow session.
    slots = _capacity("crashed", None)["slots"]
    assert [(s["key"], s["holder"], s["kind"]) for s in slots] == [("claude", "crashed:fix:1", "ordinary")]
    assert _leases("crashed")["holders"][0]["quarantined"] is False
