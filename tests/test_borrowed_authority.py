"""Borrowed execution authority requires the parent's current issued binding."""

import asyncio
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from garuda.interfaces.run_guard import WorkspaceLeaseGuard
from garuda.runtime.capacity import CapacityStore
from garuda.workspace.lease import LeaseError, LeaseStore

ROOT = Path(__file__).resolve().parents[1]
OPERATIONS = ["delegate", "guard", "acquire", "start", "race"]


@pytest.fixture(autouse=True)
def limited_capacity():
    settings = Path(os.environ["GARUDA_GLOBAL_SETTINGS"])
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text("capacity:\n  native: 1\n")


async def _use(parent, capability, borrowed, operation):
    if operation == "delegate":
        parent.delegate("new-child")
    elif operation == "guard":
        capability.guard(capacity_key="native")
    elif operation == "acquire":
        borrowed.acquire()
    elif operation == "start":
        borrowed.start_heartbeat()
    else:
        ready = asyncio.get_running_loop().create_future()
        ready.set_result("work admitted")
        await borrowed.race(ready)


CASES = [(state, op) for state in ["epoch", "missing", "unknown"] for op in OPERATIONS]
CASES += [("released", op) for op in ["acquire", "start", "race"]]


@pytest.mark.parametrize("state,operation", CASES)
async def test_capability_creation_and_use_refuse_lost_parent_authority(tmp_path, monkeypatch,
                                                                      state, operation):
    from garuda.runtime import recovery

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = LeaseStore(tmp_path / "leases")
    parent = WorkspaceLeaseGuard(str(workspace), "parent", lease_store=store)
    parent.acquire()
    capability = parent.delegate("child")
    borrowed = capability.guard(capacity_key="native")
    source, = store.root.glob("*.json")
    replacement = None
    if state == "released":
        await parent.release()
        replacement = WorkspaceLeaseGuard(str(workspace), "replacement", lease_store=store)
        replacement.acquire()
    elif state == "missing":
        source.unlink()
    elif state == "epoch":
        record = json.loads(source.read_bytes())
        record["holders"][0]["epoch"] = "replacement-epoch"
        source.write_text(json.dumps(record))
    before = source.read_bytes() if source.exists() else None
    with monkeypatch.context() as patch:
        if state == "unknown":
            patch.setattr(recovery, "_process_identity", lambda pid: None)
        with pytest.raises(LeaseError):
            await _use(parent, capability, borrowed, operation)
            (workspace / "unwanted-writer").write_text("admitted after lost authority")
    assert (source.read_bytes() if source.exists() else None) == before
    assert CapacityStore().holders("native") == []
    assert not (workspace / "unwanted-writer").exists()
    if replacement:
        assert [h.session_id for h in store.holders_of(workspace)] == ["replacement"]
        await replacement.release()


@pytest.mark.parametrize("operation", OPERATIONS)
@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires real fork inheritance")
def test_a_fork_cannot_inherit_borrowed_execution_authority(tmp_path, operation):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    code = '''
        import asyncio, os, sys
        from garuda.interfaces.run_guard import WorkspaceLeaseGuard
        from garuda.runtime.capacity import CapacityStore
        from garuda.workspace.lease import LeaseError, LeaseStore
        store=LeaseStore(sys.argv[1])
        parent=WorkspaceLeaseGuard(sys.argv[2], 'parent', lease_store=store)
        parent.acquire()
        capability=parent.delegate('child')
        borrowed=capability.guard(capacity_key='native')
        source,=store.root.glob('*.json')
        before=source.read_bytes()
        async def use():
            operation=sys.argv[3]
            if operation=='delegate': parent.delegate('new-child')
            elif operation=='guard': capability.guard(capacity_key='native')
            elif operation=='acquire': borrowed.acquire()
            elif operation=='start': borrowed.start_heartbeat()
            else:
                ready=asyncio.get_running_loop().create_future()
                ready.set_result('work admitted')
                await borrowed.race(ready)
        child=os.fork()
        if child==0:
            try: asyncio.run(use())
            except LeaseError: os._exit(3)
            except BaseException: os._exit(5)
            os._exit(4)
        _,status=os.waitpid(child,0)
        if os.waitstatus_to_exitcode(status)!=3 or source.read_bytes()!=before:
            sys.exit(4)
        if CapacityStore().holders('native'): sys.exit(6)
        # The issuer can still delegate, reserve and finish normally.
        borrowed.acquire()
        borrowed.start_heartbeat()
        asyncio.run(borrowed.release())
        asyncio.run(parent.release())
        if store.holders_of(sys.argv[2]) or CapacityStore().holders('native'): sys.exit(7)
    '''
    result = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(code), str(tmp_path / "leases"),
         str(workspace), operation], capture_output=True, text=True, timeout=30,
        env=dict(os.environ, PYTHONPATH=str(ROOT)),
    )
    assert result.returncode == 0, result.stderr


async def test_legitimate_borrowing_runs_without_renewing_or_replacing_the_parent(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = LeaseStore(tmp_path / "leases")
    parent = WorkspaceLeaseGuard(str(workspace), "parent", lease_store=store)
    parent.acquire()
    source, = store.root.glob("*.json")
    before = source.read_bytes()
    borrowed = parent.delegate("child").guard(capacity_key="native")
    borrowed.acquire()
    borrowed.start_heartbeat()

    async def work():
        (workspace / "legitimate-writer").write_text("authorized")
        return "done"

    assert await borrowed.race(work()) == "done"
    assert source.read_bytes() == before
    assert CapacityStore().holders("native") == ["child"]
    await borrowed.release()
    assert source.read_bytes() == before
    assert CapacityStore().holders("native") == []
    await parent.release()
    assert store.holders_of(workspace) == []
