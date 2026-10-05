"""Quarantine retains ownership while a real descendant keeps writing."""

import os
import signal
import subprocess
import sys
import time

import pytest

from garuda.interfaces.run_guard import WorkspaceLeaseGuard
from garuda.runtime.capacity import CapacityStore, CapacityUnavailable
from garuda.workspace.lease import LeaseConflictError, LeaseError, LeaseStore


@pytest.fixture
def writer(tmp_path):
    marker = tmp_path / "descendant-writes"
    process = subprocess.Popen(
        [sys.executable, "-c", "import sys,time\nfrom pathlib import Path\n"
         "p=Path(sys.argv[1])\nwhile True:\n"
         " with p.open('ab') as f: f.write(b'.')\n time.sleep(.02)", str(marker)],
        start_new_session=True,
    )
    deadline = time.monotonic() + 5
    while not marker.exists() and time.monotonic() < deadline:
        time.sleep(.02)
    try:
        assert marker.exists()
        yield process, marker
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=5)


def _still_writes(process, marker):
    before = marker.stat().st_size
    deadline = time.monotonic() + 5
    while marker.stat().st_size == before and time.monotonic() < deadline:
        time.sleep(.02)
    assert process.poll() is None
    assert marker.stat().st_size > before


@pytest.mark.parametrize("path", ["workspace", "borrowed-child", "borrowed-parent"])
async def test_quarantine_retains_ownership_through_later_release(tmp_path, writer, path):
    from pathlib import Path

    settings = Path(os.environ["GARUDA_GLOBAL_SETTINGS"])
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text("capacity:\n  native: 1\n  codex: 1\n")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    parent = WorkspaceLeaseGuard(str(workspace), "parent", capacity_key="native")
    parent.acquire()
    parent.start_heartbeat()
    capability = parent.delegate("child") if path != "workspace" else None
    child = capability.guard(capacity_key="codex") if capability else None
    if child:
        child.acquire()
        child.start_heartbeat()
    # Each writer has its own process group; cancellation of a Python awaitable
    # is not evidence that this real descendant has stopped.
    process, marker = writer
    await (child if path == "borrowed-child" else parent).stop_heartbeat()
    # Exercise both cleanup orders. A flow finalizer releases the parent after
    # its step, while other callers can close the parent before child teardown.
    if path == "borrowed-parent":
        await parent.release()
    if child:
        await child.release()
    await parent.release()
    _still_writes(process, marker)
    capacity = CapacityStore()
    for key in (["native", "codex"] if child else ["native"]):
        with pytest.raises(CapacityUnavailable):
            capacity.reserve(key, "another-workspace", 1)
    with pytest.raises(LeaseConflictError):
        LeaseStore().acquire(workspace, "another-editor")
    assert [h.session_id for h in LeaseStore().holders_of(workspace)] == ["parent"]
    _still_writes(process, marker)
    with pytest.raises(LeaseError):
        parent.acquire()
    # A later lease refusal must not mask capacity being returned by cleanup.
    assert capacity.holders("native") == ["parent"]
    assert [h.session_id for h in LeaseStore().holders_of(workspace)] == ["parent"]
    with pytest.raises(LeaseError):
        parent.delegate("new-child")
    if capability:
        with pytest.raises(LeaseError):
            capability.guard(capacity_key="codex")
        with pytest.raises(LeaseError):
            child.acquire()
        with pytest.raises(LeaseError):
            child.start_heartbeat()
        parent.revoke(capability)
        await child.release()
        await parent.release()
        assert capacity.holders("codex") == ["child"]
        assert [h.session_id for h in LeaseStore().holders_of(workspace)] == ["parent"]


async def test_reaped_completion_releases_borrowed_and_parent_ownership(tmp_path, writer):
    from pathlib import Path

    settings = Path(os.environ["GARUDA_GLOBAL_SETTINGS"])
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text("capacity:\n  native: 1\n  codex: 1\n")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    parent = WorkspaceLeaseGuard(str(workspace), "parent", capacity_key="native")
    parent.acquire()
    capability = parent.delegate("child")
    child = capability.guard(capacity_key="codex")
    child.acquire()
    process, _ = writer
    os.killpg(process.pid, signal.SIGKILL)
    process.wait(timeout=5)
    parent.revoke(capability)
    await child.release()
    await parent.release()
    assert LeaseStore().holders_of(workspace) == []
    capacity = CapacityStore()
    assert capacity.holders("native") == capacity.holders("codex") == []
    replacement = WorkspaceLeaseGuard(str(workspace), "replacement", capacity_key="native")
    replacement.acquire()
    await replacement.release()
