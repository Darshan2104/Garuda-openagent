"""Authenticated admission lending at the real workspace-guard boundary."""

import copy
import json
import os
import subprocess
import sys
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest

from garuda.interfaces.run_guard import WorkspaceLeaseGuard
from garuda.runtime.capacity import CapacityError, CapacityStore, CapacityUnavailable
from garuda.runtime.capacity_loan import CapacityLoan
from garuda.workspace.lease import LeaseStore


def setup(tmp_path, *, lend=True):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = CapacityStore(tmp_path / "capacity")
    reservation = store.reserve("acp", "child", 1)
    quarantined = []
    loan = (CapacityLoan(store, reservation, quarantine=lambda: quarantined.append(True))
            if lend else None)
    return workspace, store, reservation, loan, quarantined


@pytest.mark.parametrize("damage", ["copied-reservation", "foreign-store", "copied-loan",
                                    "stale", "replaced", "epoch", "runtime", "session",
                                    "fake-loan", "fake-store"])
def test_invalid_capacity_authority_cannot_acquire_a_workspace(tmp_path, damage):
    workspace, store, reservation, loan, _ = setup(
        tmp_path, lend=damage not in {"copied-reservation", "foreign-store"})
    key, holder = "acp", "child"
    if damage == "fake-store":
        forged = SimpleNamespace(root=store.root, _load=store._load,
                                 _check_issued=lambda *_: None, _loans={})
        with pytest.raises(CapacityError):
            CapacityLoan(forged, reservation, quarantine=lambda: None)
        assert store.holders("acp") == ["child"]
        return
    if damage == "fake-loan":
        loan = SimpleNamespace(acquire=lambda *_: None, release=lambda: None,
                               quarantine=lambda: None)
    if damage in {"copied-reservation", "foreign-store"}:
        issuer = store if damage == "copied-reservation" else CapacityStore(store.root)
        offered = replace(reservation) if damage == "copied-reservation" else reservation
        with pytest.raises(CapacityError):
            CapacityLoan(issuer, offered, quarantine=lambda: None)
        assert store.holders("acp") == ["child"]
        return
    if damage == "copied-loan":
        loan = copy.copy(loan)
    if damage in {"stale", "replaced"}:
        store.release(reservation)
        if damage == "replaced":
            store.reserve("acp", "child", 1)
    if damage == "epoch":
        from garuda.runtime.strict_store import write_document

        (path,) = store.root.glob("*.json")
        document = json.loads(path.read_text())
        document["slots"]["child"]["owner"]["epoch"] = "b" * 32
        write_document(path, document)
    if damage == "runtime":
        key = "other"
    if damage == "session":
        holder = "foreign"
    expected = [] if damage == "stale" else ["child"]
    guard = WorkspaceLeaseGuard(str(workspace), holder, capacity_key=key, capacity_loan=loan)
    with pytest.raises(CapacityError):
        guard.acquire()
    assert store.holders("acp") == expected
    assert LeaseStore().holders_of(workspace) == []


@pytest.mark.skipif(not hasattr(os, "fork"), reason="needs a real fork")
def test_a_fork_cannot_borrow_the_parents_issued_slot(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    code = """
import os,sys,json
from garuda.interfaces.run_guard import WorkspaceLeaseGuard
from garuda.runtime.capacity import CapacityError,CapacityStore
from garuda.runtime.capacity_loan import CapacityLoan
from garuda.workspace.lease import LeaseStore
store=CapacityStore(sys.argv[1]); reservation=store.reserve('acp','child',1)
loan=CapacityLoan(store,reservation,quarantine=lambda:None)
pid=os.fork()
if pid==0:
    try:
        WorkspaceLeaseGuard(sys.argv[2],'child',capacity_key='acp',capacity_loan=loan).acquire()
    except CapacityError:
        os._exit(0)
    os._exit(1)
_,status=os.waitpid(pid,0)
print(json.dumps({'exit':os.waitstatus_to_exitcode(status),'holders':store.holders('acp'),
                  'leases':len(LeaseStore().holders_of(sys.argv[2]))}))
"""
    result = subprocess.run([sys.executable, "-c", code, str(tmp_path / "capacity"),
                             str(workspace)], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"exit": 0, "holders": ["child"], "leases": 0}


async def wait_marker(marker, after=-1):
    import asyncio

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if marker.exists() and int(marker.read_text() or "0") > after:
            return int(marker.read_text())
        await asyncio.sleep(0.02)
    raise AssertionError("real writer did not advance")


@pytest.mark.parametrize("unknown", [False, True])
async def test_borrower_cleanup_keeps_parent_release_authority(tmp_path, unknown):
    workspace, store, reservation, loan, quarantined = setup(tmp_path)
    guard = WorkspaceLeaseGuard(str(workspace), "child", capacity_key="acp", capacity_loan=loan)
    guard.acquire()
    marker = tmp_path / "marker"
    writer = subprocess.Popen([sys.executable, "-c",
                               "import os,sys,time\nfrom pathlib import Path\n"
                               "p=Path(sys.argv[1])\nn=0\nwhile True:\n"
                               " n+=1; t=p.with_suffix('.next'); t.write_text(str(n));"
                               " os.replace(t,p); time.sleep(.03)\n", str(marker)])
    try:
        before = await wait_marker(marker)
        assert store.holders("acp") == ["child"]
        with pytest.raises(CapacityUnavailable):
            CapacityStore(store.root).reserve("acp", "competitor", 1)
        store.release(reservation)
        CapacityStore(store.root).release(reservation)
        assert store.holders("acp") == ["child"]  # an active borrow cannot free its parent
        if unknown:
            await guard.stop_heartbeat()
            await guard.release()
            loan.release()
            store.release(reservation)
            CapacityStore(store.root).release(reservation)
            assert await wait_marker(marker, before) > before
            assert quarantined == [True]
            assert store.holders("acp") == ["child"]
        else:
            writer.terminate()
            writer.wait(timeout=5)
            await guard.release()
            assert LeaseStore().holders_of(workspace) == []
            CapacityStore(store.root).release(reservation)
            assert store.holders("acp") == ["child"]  # only issuing owner finalizes admission
            store.release(replace(reservation))
            assert store.holders("acp") == ["child"]  # copied metadata is not owner authority
            store.release(reservation)
            assert store.holders("acp") == [] and quarantined == []
            with pytest.raises(CapacityError):
                guard.acquire()  # a completed loan cannot be reused
    finally:
        if writer.poll() is None:
            writer.terminate()
        writer.wait(timeout=5)
    if unknown:
        store.release(reservation)
        CapacityStore(store.root).release(reservation)
        assert store.holders("acp") == ["child"]  # later writer death is not a cleanup receipt
