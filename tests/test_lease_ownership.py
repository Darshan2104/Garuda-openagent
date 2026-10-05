"""A session ID and a copied epoch cannot authorize workspace lease mutation."""

import asyncio
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
from garuda.workspace.lease import LeaseConflictError, LeaseError, LeaseStore

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def live_owner(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    root = tmp_path / "leases"
    code = """
        import asyncio, json, sys, threading, time
        from pathlib import Path
        from garuda.interfaces.run_guard import WorkspaceLeaseGuard
        from garuda.workspace.lease import LeaseStore
        workspace, root = Path(sys.argv[1]), Path(sys.argv[2])
        leases = LeaseStore(root)
        guard = WorkspaceLeaseGuard(str(workspace), 'same', lease_store=leases)
        guard.acquire()
        holder, = leases.holders_of(workspace)
        stop = threading.Event()
        def write():
            while not stop.is_set():
                with (workspace / 'original-writer').open('ab') as f: f.write(b'.')
                time.sleep(.02)
        writer = threading.Thread(target=write)
        writer.start()
        print(json.dumps(holder.to_dict()), flush=True)
        try:
            sys.stdin.readline()
        finally:
            stop.set()
            writer.join()
            asyncio.run(guard.release())
    """
    parent = subprocess.Popen(
        [sys.executable, "-c", textwrap.dedent(code), str(workspace), str(root)],
        env=dict(os.environ, PYTHONPATH=str(ROOT)), stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True,
    )
    try:
        record = json.loads(parent.stdout.readline())
        marker = workspace / "original-writer"
        deadline = time.monotonic() + 5
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(.02)
        assert marker.exists()
        source, = root.glob("*.json")
        yield workspace, root, source, marker, record, parent
    finally:
        if parent.poll() is None:
            try:
                parent.communicate("release\n", timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(parent.pid, signal.SIGKILL)
                parent.communicate(timeout=5)


@pytest.mark.parametrize("operation,copied_epoch", [
    ("acquire-mutating", False), ("acquire-read-only", False),
    ("heartbeat", False), ("release", False), ("heartbeat", True), ("release", True),
])
def test_a_foreign_live_lease_survives_same_session_operations(live_owner, operation, copied_epoch):
    workspace, root, source, marker, record, parent = live_owner
    before = source.read_bytes()
    written = marker.stat().st_size
    store = LeaseStore(root)
    with pytest.raises(LeaseConflictError):
        if operation.startswith("acquire-"):
            guard = WorkspaceLeaseGuard(str(workspace), "same", lease_store=store,
                                        mode=operation.removeprefix("acquire-"))
            guard.acquire()
            (workspace / "unproved-new-writer").write_text("unwanted admission")
        else:
            getattr(store, operation)(workspace, "same", epoch=record["epoch"] if copied_epoch else None)
    assert source.read_bytes() == before
    assert not (workspace / "unproved-new-writer").exists()
    deadline = time.monotonic() + 5
    while marker.stat().st_size == written and time.monotonic() < deadline:
        time.sleep(.02)
    assert parent.poll() is None
    assert marker.stat().st_size > written


@pytest.mark.parametrize("operation", ["heartbeat", "release"])
def test_an_unowned_instance_in_the_same_process_cannot_use_a_copied_epoch(tmp_path, operation):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    issuer = LeaseStore(tmp_path / "leases")
    holder = issuer.acquire(workspace, "same")
    source, = issuer.root.glob("*.json")
    before = source.read_bytes()
    with pytest.raises(LeaseConflictError):
        getattr(LeaseStore(issuer.root), operation)(workspace, "same", epoch=holder.epoch)
    assert source.read_bytes() == before
    issuer.heartbeat(workspace, "same", epoch=holder.epoch)
    issuer.release(workspace, "same", epoch=holder.epoch)
    assert issuer.holders_of(workspace) == []


@pytest.mark.parametrize("operation", ["heartbeat", "release"])
@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires real fork inheritance")
def test_a_fork_cannot_inherit_its_parents_lease_mutation_authority(tmp_path, operation):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    root = tmp_path / "leases"
    code = """
        import os, sys
        from pathlib import Path
        from garuda.workspace.lease import LeaseConflictError, LeaseStore
        issuer = LeaseStore(sys.argv[1])
        holder = issuer.acquire(sys.argv[2], 'same')
        source, = issuer.root.glob('*.json')
        before = source.read_bytes()
        child = os.fork()
        if child == 0:
            try:
                getattr(issuer, sys.argv[3])(sys.argv[2], 'same', epoch=holder.epoch)
            except LeaseConflictError:
                os._exit(3)
            except BaseException:
                os._exit(5)
            os._exit(4)
        _, status = os.waitpid(child, 0)
        if os.waitstatus_to_exitcode(status) != 3 or source.read_bytes() != before:
            sys.exit(4)
        issuer.heartbeat(sys.argv[2], 'same', epoch=holder.epoch)
        issuer.release(sys.argv[2], 'same', epoch=holder.epoch)
    """
    parent = subprocess.run([sys.executable, "-c", textwrap.dedent(code), str(root),
                             str(workspace), operation], capture_output=True, text=True,
                            env=dict(os.environ, PYTHONPATH=str(ROOT)), timeout=30)
    assert parent.returncode == 0, parent.stderr
    assert LeaseStore(root).holders_of(workspace) == []


def test_the_issuing_owner_can_release_before_another_owner_reuses_the_session(live_owner):
    workspace, root, _, _, _, parent = live_owner
    parent.communicate("release\n", timeout=5)
    assert parent.returncode == 0
    store = LeaseStore(root)
    assert store.holders_of(workspace) == []
    replacement = WorkspaceLeaseGuard(str(workspace), "same", lease_store=store)
    replacement.acquire()
    (workspace / "proved-new-writer").write_text("legitimate admission")
    asyncio.run(replacement.release())
    assert store.holders_of(workspace) == []


def test_unknown_creator_identity_cannot_publish_a_lease(tmp_path, monkeypatch):
    from garuda.runtime import recovery

    monkeypatch.setattr(recovery, "_process_identity", lambda pid: None)
    store = LeaseStore(tmp_path / "leases")
    with pytest.raises(LeaseError):
        store.acquire(tmp_path, "unknown")
    assert not store.root.exists()


@pytest.mark.parametrize("operation", ["heartbeat", "release"])
@pytest.mark.parametrize("field", ["pid", "identity", "pgid", "epoch", "mode", "workspace", "duplicate"])
def test_a_retained_handle_cannot_mutate_a_disagreeing_owner_record(tmp_path, operation, field):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    issuer = LeaseStore(tmp_path / "leases")
    issuer.acquire(workspace, "same")
    source, = issuer.root.glob("*.json")
    document = json.loads(source.read_bytes())
    entry = document["holders"][0]
    if field == "duplicate":
        document["holders"].append({**entry, "epoch": "another-owner"})
    elif field == "mode":
        entry[field] = "read-only"
    elif field == "workspace":
        entry[field] = str(tmp_path / "other-workspace")
    elif field == "epoch":
        entry[field] = "another-epoch"
    else:
        entry[field] = "another-birth" if field == "identity" else entry[field] + 1
    source.write_text(json.dumps(document))
    before = source.read_bytes()
    with pytest.raises(LeaseConflictError):
        getattr(issuer, operation)(workspace, "same", epoch=entry["epoch"])
    assert source.read_bytes() == before


@pytest.mark.parametrize("operation", ["heartbeat", "release"])
@pytest.mark.parametrize("missing_record", [False, True])
def test_a_released_handle_cannot_regain_authority_from_restored_bytes(tmp_path, operation,
                                                                     missing_record):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    issuer = LeaseStore(tmp_path / "leases")
    holder = issuer.acquire(workspace, "same")
    source, = issuer.root.glob("*.json")
    previous = source.read_bytes()
    if missing_record:
        source.unlink()
    issuer.release(workspace, "same", epoch=holder.epoch)
    source.write_bytes(previous)
    with pytest.raises(LeaseConflictError):
        getattr(issuer, operation)(workspace, "same", epoch=holder.epoch)
    assert source.read_bytes() == previous


def test_a_failed_publication_cannot_authorize_mutation_of_its_visible_record(tmp_path, monkeypatch):
    import stat

    from garuda.runtime import strict_store

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    issuer = LeaseStore(tmp_path / "leases")
    fsync = strict_store.os.fsync

    def failed_directory_flush(fd):
        fsync(fd)
        if stat.S_ISDIR(os.fstat(fd).st_mode) and list(issuer.root.glob("*.json")):
            raise OSError("injected post-publication directory flush failure")

    with monkeypatch.context() as patch:
        patch.setattr(strict_store.os, "fsync", failed_directory_flush)
        with pytest.raises((LeaseError, OSError)) as failure:
            issuer.acquire(workspace, "uncommitted")
    source, = issuer.root.glob("*.json")
    before = source.read_bytes()
    for operation in ("heartbeat", "release"):
        with pytest.raises(LeaseConflictError):
            getattr(issuer, operation)(workspace, "uncommitted")
        assert source.read_bytes() == before
    assert isinstance(failure.value, LeaseError)
