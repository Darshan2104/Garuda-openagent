"""Protected metadata uses the same cross-process owner as ordinary writers."""

import os
import shutil
import stat
import subprocess
import sys
import threading
from contextlib import contextmanager

import pytest

from garuda.core import session_publication as publication
from garuda.core.sessions import SessionStore, merge_meta
from garuda.runtime.strict_store import StorageError


def test_legacy_metadata_keeps_symlinked_store_compatibility(tmp_path):
    target = tmp_path / "real-store"
    target.mkdir()
    link = tmp_path / "linked-store"
    link.symlink_to(target, target_is_directory=True)
    store = SessionStore(link)
    store.begin("legacy", task="initial", model="test", agent="test", workspace=str(tmp_path))
    # A protected session in the same real store must not break ordinary IDs.
    strict = SessionStore(target)
    strict.protect_publication("flow")
    strict.begin("flow", task="protected", model="test", agent="test", workspace=str(tmp_path))
    store.mutate_meta("legacy", lambda current: {"task": current["task"] + " updated"})
    assert store.load_meta("legacy")["task"] == "initial updated"
    assert [meta["session_id"] for meta in store.list_sessions()] == ["legacy"]
    assert strict.load_meta("flow")["task"] == "protected"
    with pytest.raises((StorageError, OSError)):
        store.update_meta("flow", {"task": "downgraded"})


def test_legacy_writer_cannot_publish_after_policy_is_installed(tmp_path, monkeypatch):
    store = SessionStore(tmp_path / "sessions")
    store.session_dir("flow").mkdir(mode=0o700, parents=True)
    selected = threading.Event()
    proceed = threading.Event()
    failures = []
    original = publication.publication_cycle

    @contextmanager
    def paused_cycle(path, **kwargs):
        with original(path, **kwargs) as cycle:
            if cycle is None:
                selected.set()
                assert proceed.wait(5)
            yield cycle

    monkeypatch.setattr(publication, "publication_cycle", paused_cycle)

    def writer():
        try:
            store.begin("flow", task="stale writer", model="test", agent="test", workspace=str(tmp_path))
        except BaseException as exc:
            failures.append(exc)

    thread = threading.Thread(target=writer)
    thread.start()
    try:
        assert selected.wait(5)
        store.protect_publication("flow")
    finally:
        proceed.set()
        thread.join(timeout=5)
    assert not thread.is_alive()
    assert len(failures) == 1 and isinstance(failures[0], StorageError)
    assert not (store.session_dir("flow") / "meta.json").exists()
    monkeypatch.setattr(publication, "publication_cycle", original)
    store.begin("flow", task="protected writer", model="test", agent="test", workspace=str(tmp_path))
    assert store.load_meta("flow")["task"] == "protected writer"


@pytest.fixture
def protected(tmp_path):
    store = SessionStore(tmp_path / "sessions")
    store.protect_publication("flow")
    store.begin("flow", task="verify", model="flow", agent="flow:build-review",
                workspace=str(tmp_path))
    return store


@pytest.mark.parametrize("fault", ["marker", "lock", "initialized", "head", "meta",
                                  "replaced-lock", "replaced-meta", "corrupt-meta",
                                  "directory", "registry", "all-local-records"])
def test_every_shared_metadata_writer_refuses_lost_or_replaced_policy(protected, fault):
    store = protected
    directory = store.session_dir("flow")
    meta = directory / "meta.json"
    targets = {"marker": publication.MARKER, "lock": "meta.json.lock",
               "initialized": publication.INITIALIZED, "head": publication.HEAD,
               "meta": "meta.json"}
    if fault in targets:
        (directory / targets[fault]).unlink()
    elif fault in ("replaced-lock", "replaced-meta"):
        path = directory / ("meta.json.lock" if fault == "replaced-lock" else "meta.json")
        temporary = directory / "replacement"
        temporary.write_bytes(path.read_bytes())
        temporary.chmod(0o600)
        os.replace(temporary, path)
    elif fault == "corrupt-meta":
        meta.write_text('{"broken":')
    elif fault == "directory":
        old = directory.with_name("old-flow")
        directory.rename(old)
        shutil.copytree(old, directory)
    elif fault == "registry":
        shutil.rmtree(store.root / publication.REGISTRY)
    else:
        for path in directory.iterdir():
            if path.is_file():
                path.unlink()
    before = meta.read_bytes() if meta.exists() else None
    operations = [lambda: store.update_meta("flow", {"status": "completed"}),
                  lambda: store.mutate_meta("flow", lambda _current: {"status": "completed"}),
                  lambda: merge_meta(meta, {"status": "completed"}),
                  lambda: store.begin("flow", task="replacement", model="flow", agent="flow:x",
                                      workspace=str(directory)),
                  lambda: store.load_meta("flow")]
    for operation in operations:
        with pytest.raises((StorageError, OSError)):
            operation()
        assert (meta.read_bytes() if meta.exists() else None) == before
    assert store.list_sessions() == []


def test_protected_metadata_writers_obey_the_existing_sidecar_lock(protected):
    store = protected
    holder = subprocess.Popen([sys.executable, "-c", """
import fcntl,pathlib,sys
with open(pathlib.Path(sys.argv[1])/'flow'/'meta.json.lock','r+') as lock:
    fcntl.flock(lock,fcntl.LOCK_EX)
    print('locked',flush=True)
    sys.stdin.readline()
""", str(store.root)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    writer = None
    try:
        assert holder.stdout.readline().strip() == "locked"
        writer = subprocess.Popen([sys.executable, "-c", """
import sys
from garuda.core.sessions import SessionStore
print('writing',flush=True)
SessionStore(sys.argv[1]).mutate_meta('flow',lambda current:{'observed_task':current['task']})
""", str(store.root)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        assert writer.stdout.readline().strip() == "writing"
        with pytest.raises(subprocess.TimeoutExpired):
            writer.wait(timeout=.15)
        holder.stdin.write("release\n")
        holder.stdin.flush()
        assert holder.wait(timeout=10) == 0
        _out, errors = writer.communicate(timeout=10)
        assert writer.returncode == 0, errors
        assert store.load_meta("flow")["observed_task"] == "verify"
    finally:
        for process in (writer, holder):
            if process is not None and process.poll() is None:
                process.kill()
                process.wait(timeout=10)


def test_missing_posix_locking_cannot_fall_back_to_unlocked_writes(protected, monkeypatch):
    monkeypatch.setattr(publication, "fcntl", None)
    with pytest.raises(StorageError, match="cross-process"):
        protected.update_meta("flow", {"status": "completed"})


@pytest.mark.parametrize("fault", ["lock", "file-fsync", "metadata-rename",
                                  "metadata-directory-fsync", "head-rename",
                                  "head-directory-fsync"])
def test_durability_failure_never_reports_success_or_uses_legacy_writer(protected, monkeypatch, fault):
    from garuda.core import sessions

    meta = protected.session_dir("flow") / "meta.json"
    before = meta.read_bytes()
    real_fsync, real_replace = os.fsync, os.replace
    published = set()

    def fail():
        raise OSError("injected publication failure")

    def fsync(fd):
        is_directory = stat.S_ISDIR(os.fstat(fd).st_mode)
        if (fault == "file-fsync" and not is_directory
                or fault == "metadata-directory-fsync" and is_directory and "meta.json" in published
                or fault == "head-directory-fsync" and is_directory and publication.HEAD in published):
            fail()
        return real_fsync(fd)

    def replace(source, destination, **kwargs):
        if (fault == "metadata-rename" and destination == "meta.json"
                or fault == "head-rename" and destination == publication.HEAD):
            fail()
        result = real_replace(source, destination, **kwargs)
        published.add(destination)
        return result

    def legacy_write(*args):
        pytest.fail("protected metadata reached the legacy writer")

    with monkeypatch.context() as patch:
        patch.setattr(os, "fsync", fsync)
        patch.setattr(os, "replace", replace)
        patch.setattr(sessions, "_atomic_write_text", legacy_write)
        if fault == "lock":
            patch.setattr(publication.fcntl, "flock", lambda *args: fail())
        with pytest.raises((OSError, StorageError), match="publication failure"):
            protected.update_meta("flow", {"status": "completed"})

    if fault in ("lock", "file-fsync", "metadata-rename"):
        assert meta.read_bytes() == before
        assert protected.load_meta("flow")["status"] == "running"
    elif fault == "head-directory-fsync":
        # Rename has happened despite the failed durability acknowledgement.
        # The caller must reconcile; pretending the old bytes survived is wrong.
        assert protected.load_meta("flow")["status"] == "completed"
    else:
        assert meta.read_bytes() != before
        with pytest.raises(StorageError, match="publication head"):
            protected.load_meta("flow")
        with pytest.raises(StorageError):
            protected.update_meta("flow", {"status": "running"})
