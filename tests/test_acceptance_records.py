"""Producer-side publication faults and corruption, without invented receipts."""

import json
import os
import shutil
import uuid

import pytest

from garuda.core import acceptance_records as records
from garuda.runtime.ownership import current_owner
from garuda.runtime.strict_store import CorruptRecord, StorageUnavailable

LIMITS = {"timeout": 1, "phase_timeout": 10, "term_grace": 0.1, "kill_grace": 0.1}


@pytest.fixture
def store(tmp_path):
    parent = tmp_path.resolve() / str(uuid.uuid4())
    parent.mkdir(mode=0o700)
    binding = {"parent_id": parent.name, "phase_id": str(uuid.uuid4()),
               "check_id": "a" * 64, "check_authority": "user-request",
               "attempt_id": str(uuid.uuid4()), "workspace": str(tmp_path.resolve()),
               "owner": current_owner().to_dict(), "candidate": "b" * 16,
               "workspace_identity": {"device": tmp_path.stat().st_dev, "inode": tmp_path.stat().st_ino},
               "evidence_digest": "c" * 64, "authority_digest": "d" * 64,
               "ceiling_digest": "e" * 64, "request_digest": "f" * 64}
    return records.CheckRecords(str(parent), binding)


def _supervisor():
    return {key: value for key, value in current_owner().to_dict().items() if key != "epoch"}


def test_duplicate_preparation_has_one_authorization_transition(store):
    assert store.prepare("a check", LIMITS) == {"created": True, "phase": "prepared"}
    assert store.prepare("a check", LIMITS) == {"created": False, "phase": "prepared"}
    assert store.reconcile() == {"classification": "never-launched"}
    store.transition("prepared", "launch-authorized", supervisor=_supervisor())
    with pytest.raises(CorruptRecord):
        store.transition("prepared", "launch-authorized", supervisor=_supervisor())
    assert store.reconcile() == {"classification": "uncertain"}


@pytest.mark.parametrize("boundary", ["registry", "intent", "authorization"])
def test_failed_publication_cannot_undo_the_producers_actual_transition(store, monkeypatch, boundary):
    write = records.write_locked_document
    if boundary == "authorization":
        store.prepare("a check", LIMITS)

    def fail_after_publish(directory, path, document):
        write(directory, path, document)
        selected = {"registry": path.name == "request.json" and bool(document.get("attempts")),
                    "intent": document.get("phase") == "prepared",
                    "authorization": document.get("phase") == "launch-authorized"}[boundary]
        if selected:
            raise StorageUnavailable("injected failure after publication")

    with monkeypatch.context() as patch:
        patch.setattr(records, "write_locked_document", fail_after_publish)
        with pytest.raises(StorageUnavailable):
            if boundary == "authorization":
                store.transition("prepared", "launch-authorized", supervisor=_supervisor())
            else:
                store.prepare("a check", LIMITS)
    if boundary == "registry":
        assert store.reconcile() == {"classification": "uncertain"}
        with pytest.raises(CorruptRecord):
            store.prepare("a check", LIMITS)
    else:
        assert store.prepare("a check", LIMITS)["created"] is False
        assert store.reconcile()["classification"] == (
            "never-launched" if boundary == "intent" else "uncertain")


@pytest.mark.parametrize("missing", ["intent", "registry", "directory", "lock", "policy"])
def test_missing_owned_records_never_become_a_fresh_attempt(store, missing):
    store.prepare("a check", LIMITS)
    paths = {"intent": store.path / store.intent_name, "registry": store.path / "request.json",
             "directory": store.path, "lock": store.path / ".lock",
             "policy": store.parent / store.policy_name}
    path = paths[missing]
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink()
    with pytest.raises((CorruptRecord, StorageUnavailable)):
        store.prepare("a check", LIMITS)
    if missing == "lock":
        assert not path.exists()
    if missing == "directory":
        assert not path.exists()


@pytest.mark.parametrize("corruption", ["version", "duplicate", "nonfinite", "oversized", "public", "symlink", "fifo"])
def test_corrupt_producer_intent_is_preserved_and_refused(store, corruption):
    store.prepare("a check", LIMITS)
    path = store.path / store.intent_name
    if corruption == "version":
        value = json.loads(path.read_text())
        value["version"] = 99
        path.write_text(json.dumps(value))
    elif corruption == "duplicate":
        path.write_text('{"version":1,"version":1}')
    elif corruption == "nonfinite":
        value = json.loads(path.read_text())
        value["limits"]["timeout"] = float("nan")
        path.write_text(json.dumps(value))
    elif corruption == "oversized":
        path.write_bytes(b" " * 65_537)
    elif corruption == "public":
        path.chmod(0o644)
    elif corruption == "symlink":
        path.unlink()
        path.symlink_to(store.parent / store.policy_name)
    else:
        path.unlink()
        os.mkfifo(path, 0o600)
    before = path.lstat()
    with pytest.raises((CorruptRecord, StorageUnavailable)):
        store.reconcile()
    assert path.lstat().st_ino == before.st_ino
