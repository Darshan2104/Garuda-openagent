"""Protected metadata uses the same cross-process owner as ordinary writers."""

import os
import shutil
import subprocess
import sys

import pytest

from garuda.core import session_publication as publication
from garuda.core.sessions import SessionStore, merge_meta
from garuda.runtime.strict_store import StorageError


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
