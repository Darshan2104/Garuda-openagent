"""Expired workspace leases retain unproved writers after parent death."""

import json
import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from garuda.interfaces.run_guard import WorkspaceLeaseGuard
from garuda.runtime.ownership import Owner, owner_liveness
from garuda.workspace.lease import LeaseConflictError, LeaseStore

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def orphan_writer(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    root = tmp_path / "leases"
    marker = workspace / "original-writer"
    code = '''
        import json, os, subprocess, sys
        from garuda.workspace.lease import LeaseStore
        from garuda.runtime.recovery import _process_identity
        LeaseStore(sys.argv[1]).acquire(sys.argv[2], 'original', ttl_sec=1)
        writer="import sys,time; from pathlib import Path; p=Path(sys.argv[1]);\\nwhile True:\\n with p.open('ab') as f: f.write(b'.')\\n time.sleep(.02)"
        child=subprocess.Popen([sys.executable,'-c',writer,sys.argv[3]],
                               stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL,start_new_session=True)
        print(json.dumps({'pid':child.pid,'identity':_process_identity(child.pid)}),flush=True)
        os._exit(0)
    '''
    parent = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(code), str(root), str(workspace), str(marker)],
        env=dict(os.environ, PYTHONPATH=str(ROOT)), start_new_session=True,
        capture_output=True, text=True, timeout=30,
    )
    assert parent.returncode == 0, parent.stderr
    record = json.loads(parent.stdout)
    child = Owner(record["pid"], record["identity"], record["pid"], "child")
    try:
        deadline = time.monotonic() + 5
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(.02)
        assert marker.exists()
        store = LeaseStore(root)
        holder, = store.holders_of(workspace)
        assert owner_liveness(Owner(holder.pid, holder.identity, holder.pgid, holder.epoch).to_dict()) is False
        assert owner_liveness(child.to_dict()) is True
        source, = root.glob("*.json")
        yield workspace, store, holder, source, marker, child
    finally:
        if owner_liveness(child.to_dict()) is True:
            os.killpg(child.pid, signal.SIGKILL)


@pytest.mark.parametrize("register_reader", [False, True])
def test_parent_death_and_expiry_cannot_remove_a_live_writers_lease(orphan_writer, register_reader):
    workspace, store, original, source, marker, child = orphan_writer
    # A read-only acquisition is allowed, but must not erase another holder.
    # Using a future clock proves the expiry path without timing races.
    if register_reader:
        store.acquire(workspace, "reader", mode="read-only", now=original.heartbeat_at + 2)
        holders = {h.session_id: h for h in store.holders_of(workspace)}
        assert holders["original"] == original
        assert holders["reader"].mode == "read-only"
    before = source.read_bytes()
    written = marker.stat().st_size
    with pytest.raises(LeaseConflictError):
        if register_reader:
            WorkspaceLeaseGuard(str(workspace), "intruder", lease_store=store).acquire()
        else:
            store.acquire(workspace, "intruder", now=original.heartbeat_at + 2)
        (workspace / "new-writer").write_text("unwanted admission")
    assert source.read_bytes() == before
    assert not (workspace / "new-writer").exists()
    deadline = time.monotonic() + 5
    while marker.stat().st_size == written and time.monotonic() < deadline:
        time.sleep(.02)
    assert marker.stat().st_size > written
    assert owner_liveness(child.to_dict()) is True
