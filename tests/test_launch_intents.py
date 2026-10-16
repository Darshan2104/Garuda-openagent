"""Durable operation identity at the launch-intent storage owner (#315)."""

import json
import multiprocessing
import os
import uuid
from concurrent.futures import ProcessPoolExecutor

import pytest

from garuda.runtime.ownership import current_owner
from garuda.runtime.strict_store import CorruptRecord, StorageUnavailable
from garuda.scenarios.launch_intents import IntentConflict, IntentStateError, LaunchIntentStore


def _request(workspace, operation_id, *, request_digest="a" * 64):
    info = os.stat(workspace)
    return {"operation_id": operation_id, "request_digest": request_digest,
            "plan_digest": "b" * 64, "authority_digest": "c" * 64,
            "workspace": workspace, "workspace_identity": [info.st_dev, info.st_ino],
            "compiler_version": 1, "catalog_version": 1, "owner": current_owner()}


def _prepare_worker(root, workspace, operation_id):
    return LaunchIntentStore(root).prepare(**_request(workspace, operation_id))


@pytest.fixture
def private_store(tmp_path):
    root = tmp_path / "sessions"
    root.mkdir(mode=0o700)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return root, str(workspace), LaunchIntentStore(root)


def test_concurrent_processes_reserve_one_session_for_one_operation(private_store):
    root, workspace, store = private_store
    operation_id = str(uuid.uuid4())
    with ProcessPoolExecutor(max_workers=2, mp_context=multiprocessing.get_context("spawn")) as pool:
        futures = [pool.submit(_prepare_worker, root, workspace, operation_id) for _ in range(2)]
        results = [future.result(timeout=20) for future in futures]
    assert sorted(created for _, created in results) == [False, True]
    assert results[0][0]["reserved_session"] == results[1][0]["reserved_session"]
    assert store.read(operation_id)["phase"] == "prepared"
    with pytest.raises(IntentConflict):
        store.prepare(**_request(workspace, operation_id, request_digest="d" * 64))
    assert store.read(operation_id)["reserved_session"] == results[0][0]["reserved_session"]


def test_transition_requires_the_original_live_owner_and_preserves_uncertainty(private_store):
    root, workspace, store = private_store
    operation_id = str(uuid.uuid4())
    owner = current_owner()
    _, created = store.prepare(**{**_request(workspace, operation_id), "owner": owner})
    assert created
    with pytest.raises(IntentStateError):
        store.advance(operation_id, expected="prepared", phase="launch-authorized",
                      owner=current_owner())
    assert store.advance(operation_id, expected="prepared", phase="launch-authorized",
                         owner=owner)["sequence"] == 1
    uncertain = store.advance(operation_id, expected="launch-authorized", phase="uncertain",
                              owner=owner, reason="launch.unknown")
    assert uncertain["phase"] == "uncertain" and uncertain["sequence"] == 2
    assert store.read(operation_id) == uncertain
    with pytest.raises(IntentStateError):
        store.advance(operation_id, expected="uncertain", phase="launch-authorized", owner=owner)


def test_authorized_refusal_is_terminal_and_cannot_bind_a_session(private_store):
    _, workspace, store = private_store
    operation_id = str(uuid.uuid4())
    owner = current_owner()
    store.prepare(**{**_request(workspace, operation_id), "owner": owner})
    store.advance(operation_id, expected="prepared", phase="launch-authorized", owner=owner)
    with pytest.raises(IntentStateError):
        store.advance(operation_id, expected="launch-authorized", phase="refused",
                      owner=owner, reason="workspace.busy", session_id=str(uuid.uuid4()))
    refused = store.advance(operation_id, expected="launch-authorized", phase="refused",
                            owner=owner, reason="workspace.busy")
    assert refused["phase"] == "refused" and refused["session_id"] is None
    with pytest.raises(IntentStateError):
        store.advance(operation_id, expected="refused", phase="launch-authorized", owner=owner)


def test_read_of_absent_operation_does_not_create_store(private_store):
    root, _, store = private_store
    assert store.read(str(uuid.uuid4())) is None
    assert list(root.iterdir()) == []


@pytest.mark.parametrize("corruption", [
    "duplicate", "oversize", "unknown", "future", "wrong-id", "symlink", "perms",
])
def test_corrupt_or_redirected_records_refuse_without_repair(private_store, corruption, tmp_path):
    root, workspace, store = private_store
    operation_id = str(uuid.uuid4())
    store.prepare(**_request(workspace, operation_id))
    path = root / "launch-intents" / f"{operation_id}.json"
    if corruption == "duplicate":
        path.write_text('{"version":1,"version":1}')
    elif corruption == "oversize":
        path.write_bytes(b" " * (64 * 1024 + 1))
    elif corruption == "unknown":
        record = json.loads(path.read_text())
        record["new_authority"] = True
        path.write_text(json.dumps(record))
    elif corruption == "future":
        record = json.loads(path.read_text())
        record["version"] = 2
        path.write_text(json.dumps(record))
    elif corruption == "wrong-id":
        record = json.loads(path.read_text())
        record["operation_id"] = str(uuid.uuid4())
        path.write_text(json.dumps(record))
    elif corruption == "symlink":
        path.unlink()
        target = tmp_path / "foreign.json"
        target.write_text("{}")
        path.symlink_to(target)
    else:
        path.chmod(0o644)
    with pytest.raises(CorruptRecord):
        store.read(operation_id)
    assert path.exists()


def test_missing_or_replaced_lock_never_falls_back_to_unlocked_read(private_store):
    root, workspace, store = private_store
    operation_id = str(uuid.uuid4())
    store.prepare(**_request(workspace, operation_id))
    lock = root / "launch-intents" / ".lock"
    lock.unlink()
    with pytest.raises(StorageUnavailable):
        store.read(operation_id)


def test_symlinked_session_root_refuses_before_access(private_store, tmp_path):
    root, _, _store = private_store
    link = tmp_path / "linked-sessions"
    link.symlink_to(root, target_is_directory=True)
    with pytest.raises(StorageUnavailable):
        LaunchIntentStore(link).read(str(uuid.uuid4()))
