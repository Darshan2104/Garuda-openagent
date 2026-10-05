"""Strict storage and one shared runtime capacity (#157, plan task B.0).

Contention runs in separate OS processes through the same `WorkspaceLeaseGuard`
every launch path uses (native runner, ACP run, handoff).
"""

import json
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from garuda.interfaces.run_guard import WorkspaceLeaseGuard
from garuda.runtime import strict_store
from garuda.runtime.capacity import (
    CapacityError,
    CapacityStore,
    CapacityUnavailable,
    configured_ceiling,
)
from garuda.runtime.ownership import Owner, current_owner
from garuda.workspace.lease import LeaseConflictError, LeaseStore

ROOT = Path(__file__).resolve().parents[1]

CONTENDER = """
import json, sys, time
from garuda.interfaces.run_guard import WorkspaceLeaseGuard
from garuda.runtime.capacity import CapacityStore, CapacityUnavailable
from garuda.workspace.lease import LeaseStore
cap_root, lease_root, workspace, kind, log = sys.argv[1:6]
guard = WorkspaceLeaseGuard(
    workspace, f"{kind}-session", lease_store=LeaseStore(lease_root),
    capacity_key="codex", capacity_store=CapacityStore(cap_root), capacity_ceiling=2,
)
try:
    guard.acquire()
except CapacityUnavailable:
    sys.exit(3)
with open(log, "a") as f:
    f.write(json.dumps({"kind": kind, "event": "start", "t": time.time()}) + "\\n")
time.sleep(0.4)
with open(log, "a") as f:
    f.write(json.dumps({"kind": kind, "event": "end", "t": time.time()}) + "\\n")
import asyncio
asyncio.run(guard.release())
"""


def _peak(log: Path) -> int:
    events = sorted(
        (json.loads(line) for line in log.read_text().splitlines()),
        key=lambda e: (e["t"], 0 if e["event"] == "end" else 1),
    )
    running = peak = 0
    for event in events:
        running += 1 if event["event"] == "start" else -1
        peak = max(peak, running)
    return peak


def test_launches_of_every_kind_never_exceed_the_ceiling(tmp_path):
    kinds = ["foreground", "background", "sdk", "web", "flow-step", "consult", "handoff", "fg2"]
    log = tmp_path / "log.jsonl"
    env = dict(os.environ, PYTHONPATH=str(ROOT))
    procs = []
    for kind in kinds:
        workspace = tmp_path / kind
        workspace.mkdir()
        procs.append(
            subprocess.Popen(
                [sys.executable, "-c", textwrap.dedent(CONTENDER), str(tmp_path / "cap"),
                 str(tmp_path / "leases"), str(workspace), kind, str(log)],
                env=env,
            )
        )
    codes = [p.wait(timeout=120) for p in procs]

    assert set(codes) <= {0, 3}
    assert codes.count(0) >= 2
    assert _peak(log) <= 2
    assert CapacityStore(tmp_path / "cap").holders("codex") == []


async def test_a_held_workspace_gives_the_slot_back(tmp_path):
    caps, leases = CapacityStore(tmp_path / "cap"), LeaseStore(tmp_path / "leases")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    leases.acquire(workspace, "owner", "mutating")
    guard = WorkspaceLeaseGuard(
        str(workspace), "second", lease_store=leases,
        capacity_key="claude", capacity_store=caps, capacity_ceiling=1,
    )

    with pytest.raises(LeaseConflictError):
        guard.acquire()
    assert caps.holders("claude") == []


async def test_release_returns_the_slot(tmp_path):
    caps = CapacityStore(tmp_path / "cap")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    guard = WorkspaceLeaseGuard(
        str(workspace), "s", lease_store=LeaseStore(tmp_path / "leases"),
        capacity_key="claude", capacity_store=caps, capacity_ceiling=1,
    )
    guard.acquire()
    assert caps.holders("claude") == ["s"]
    await guard.release()
    assert caps.holders("claude") == []


def test_parent_death_without_cleanup_evidence_keeps_the_slot(tmp_path):
    code = (
        "import sys; from garuda.runtime.capacity import CapacityStore; "
        "CapacityStore(sys.argv[1]).reserve('codex', 'gone', 1)"
    )
    subprocess.run([sys.executable, "-c", code, str(tmp_path / "cap")], check=True,
                   env=dict(os.environ, PYTHONPATH=str(ROOT)))
    caps = CapacityStore(tmp_path / "cap")
    assert caps.holders("codex") == ["gone"]

    source, = caps.root.glob("*.json")
    before = source.read_bytes()
    with pytest.raises(CapacityUnavailable):
        caps.reserve("codex", "next", 1)
    assert caps.holders("codex") == ["gone"]
    assert source.read_bytes() == before


@pytest.mark.parametrize("liveness", [lambda owner: True, lambda owner: None], ids=["alive", "unknown"])
def test_a_live_or_unknown_owner_keeps_its_slot(tmp_path, liveness):
    caps = CapacityStore(tmp_path / "cap", liveness=liveness)
    caps.reserve("codex", "holder", 1)
    with pytest.raises(CapacityUnavailable):
        caps.reserve("codex", "next", 1)


@pytest.mark.parametrize("ceiling", [1, 2])
def test_another_process_cannot_replace_a_live_session_reservation(tmp_path, ceiling):
    caps = CapacityStore(tmp_path / "cap")
    original = caps.reserve("codex", "same-session", ceiling)
    workspace = tmp_path / "other-workspace"
    workspace.mkdir()
    sentinel = workspace / "started"
    code = """
        import sys
        from pathlib import Path
        from garuda.interfaces.run_guard import WorkspaceLeaseGuard
        from garuda.runtime.capacity import CapacityStore, CapacityUnavailable
        from garuda.workspace.lease import LeaseStore
        root, workspace, ceiling = sys.argv[1:]
        guard = WorkspaceLeaseGuard(
            workspace, "same-session", lease_store=LeaseStore(Path(root) / "leases"),
            capacity_key="codex", capacity_store=CapacityStore(root),
            capacity_ceiling=int(ceiling),
        )
        try:
            guard.acquire()
        except CapacityUnavailable:
            sys.exit(3)
        Path(workspace, "started").write_text("unwanted launch")
    """
    result = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(code), str(tmp_path / "cap"),
         str(workspace), str(ceiling)],
        env=dict(os.environ, PYTHONPATH=str(ROOT)), capture_output=True, text=True,
        timeout=30,
    )
    assert result.returncode == 3, result.stderr
    assert not sentinel.exists()
    caps.release(original)
    assert caps.holders("codex") == []


def test_an_unknown_owner_cannot_be_replaced_using_its_holder_id(tmp_path):
    caps = CapacityStore(tmp_path / "cap", liveness=lambda owner: None)
    original = caps.reserve("codex", "s", 2)
    with pytest.raises(CapacityUnavailable):
        caps.reserve("codex", "s", 2)
    caps.release(original)
    assert caps.holders("codex") == []


def test_reserving_with_the_same_owner_is_idempotent(tmp_path):
    caps = CapacityStore(tmp_path / "cap")
    original = caps.reserve("codex", "s", 1)
    repeated = caps.reserve("codex", "s", 1, owner=original.owner)
    assert repeated == original
    caps.release(original)
    assert caps.holders("codex") == []


def test_a_short_os_write_still_publishes_one_complete_document(tmp_path, monkeypatch):
    path = tmp_path / "store" / "document.json"
    write = os.write

    def short_write(fd, data):
        return write(fd, data[:max(1, len(data) // 2)])

    with monkeypatch.context() as interrupted:
        interrupted.setattr(os, "write", short_write)
        strict_store.write_document(path, {"version": 1, "value": "complete payload " * 64})
    assert strict_store.read_document(path, versions=(1,)) == {
        "version": 1, "value": "complete payload " * 64}


def test_a_matching_epoch_without_the_owner_identity_cannot_release(tmp_path):
    from garuda.runtime.capacity import Reservation

    caps = CapacityStore(tmp_path / "cap")
    original = caps.reserve("codex", "s", 1)
    owner = original.owner
    foreign = Owner(owner.pid, owner.identity + "-other-start", owner.pgid, owner.epoch)
    caps.release(Reservation("codex", "s", foreign))
    assert caps.holders("codex") == ["s"]
    caps.release(original)
    assert caps.holders("codex") == []


def test_a_superseded_reservation_does_not_free_the_new_one(tmp_path):
    caps = CapacityStore(tmp_path / "cap")
    code = """
        import json, sys
        from garuda.runtime.capacity import CapacityStore
        reservation = CapacityStore(sys.argv[1]).reserve("codex", "s", 1)
        print(json.dumps(reservation.owner.to_dict()))
    """
    result = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(code), str(tmp_path / "cap")],
        check=True, capture_output=True, text=True, timeout=30,
        env=dict(os.environ, PYTHONPATH=str(ROOT)),
    )
    from garuda.runtime.capacity import Reservation

    old = Reservation("codex", "s", Owner(**json.loads(result.stdout)))
    # This controlled parent started no runtime children. Its coordinator
    # explicitly releases the old receipt; parent death alone cannot do that.
    caps.release(old)
    new = caps.reserve("codex", "s", 1)
    caps.release(old)
    assert caps.holders("codex") == ["s"]
    caps.release(new)
    assert caps.holders("codex") == []


def test_ceilings_come_from_user_settings(tmp_path, monkeypatch):
    settings = tmp_path / "home" / "settings.yaml"
    settings.parent.mkdir()
    monkeypatch.setenv("GARUDA_GLOBAL_SETTINGS", str(settings))
    assert configured_ceiling("native") is None  # unset: not limited
    settings.write_text("capacity:\n  native: 1\n")
    assert configured_ceiling("native") == 1
    settings.write_text("capacity:\n  native: 0\n")
    with pytest.raises(CapacityError):
        configured_ceiling("native")


async def test_an_unlimited_runtime_reserves_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("GARUDA_GLOBAL_SETTINGS", str(tmp_path / "home" / "settings.yaml"))
    caps = CapacityStore(tmp_path / "cap")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    guard = WorkspaceLeaseGuard(
        str(workspace), "s", lease_store=LeaseStore(tmp_path / "leases"),
        capacity_key="native", capacity_store=caps,
    )
    guard.acquire()
    assert not (tmp_path / "cap").exists()
    await guard.release()


def test_a_crash_while_writing_leaves_the_previous_record(tmp_path, monkeypatch):
    path = tmp_path / "store" / "doc.json"
    strict_store.write_document(path, {"version": 1, "value": "old"})

    def crash(src, dst):
        raise OSError("simulated crash before rename")

    monkeypatch.setattr(strict_store.os, "replace", crash)
    with pytest.raises(OSError):
        strict_store.write_document(path, {"version": 1, "value": "new"})
    monkeypatch.undo()

    assert strict_store.read_document(path, versions=(1,))["value"] == "old"
    assert [p.name for p in path.parent.iterdir() if p.name != ".lock"] == ["doc.json"]


@pytest.mark.parametrize("content", ["{not json", '{"version": 7}', "[1, 2]"], ids=["corrupt", "future", "not-a-mapping"])
def test_unreadable_records_refuse_and_are_kept(tmp_path, content):
    path = tmp_path / "doc.json"
    path.write_text(content)
    with pytest.raises(strict_store.CorruptRecord):
        strict_store.read_document(path, versions=(1,))
    assert path.read_text() == content  # preserved for diagnosis


def test_the_owner_identity_distinguishes_this_process():
    a, b = current_owner(), current_owner()
    assert a.pid == b.pid == os.getpid()
    assert a.identity == b.identity and a.epoch != b.epoch


def test_capacity_waits_for_no_one(tmp_path):
    caps = CapacityStore(tmp_path / "cap")
    caps.reserve("codex", "a", 1)
    started = time.monotonic()
    with pytest.raises(CapacityUnavailable):
        caps.reserve("codex", "b", 1)
    assert time.monotonic() - started < 2


def test_garuda_run_reports_a_full_runtime_as_a_refusal(tmp_path, monkeypatch, capsys):
    import garuda.interfaces.main as main

    async def full(args):
        raise CapacityUnavailable("runtime 'native' is at its capacity of 1")

    monkeypatch.setattr(main, "run_task", full)
    args = main.build_parser().parse_args(["run", "-t", "x", "--workspace", str(tmp_path)])

    assert main._run_with_runtime_gate(args) == 2
    assert "at its capacity" in capsys.readouterr().err
