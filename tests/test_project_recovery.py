"""`garuda doctor --recover-project-ids` (#157, plan task B.1)."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from garuda.core import project_recovery as recovery
from garuda.core.project_identity import ProjectKeyMissing
from garuda.core.project_recovery import RecoveryRefused, recover_project_ids
from garuda.core.sessions import SessionStore
from garuda.workspace.lease import LeaseStore


def _repo(path: Path, history: str = "original") -> Path:
    """A repository with one commit; ``history`` makes its root commit its own."""
    path.mkdir(parents=True)
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_AUTHOR_NAME": "t",
           "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", "init", "-q", str(path)], check=True, capture_output=True, env=env)
    (path / "README").write_text(f"{path.name}: {history}\n")
    subprocess.run(["git", "-C", str(path), "add", "README"], check=True, capture_output=True,
                   env=env)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", history], check=True,
                   capture_output=True, env=env)
    return path


def _begin(store, sid, ws, **kw):
    store.begin(sid, task="do the thing", model="m", agent="a", workspace=str(ws), **kw)
    return store.load_meta(sid)


@pytest.fixture
def lost(tmp_path):
    """Two projects with named sessions, then the key is lost."""
    store = SessionStore(tmp_path / "sessions")
    a, b = _repo(tmp_path / "a"), _repo(tmp_path / "b")
    old_a = _begin(store, "sa", a, name="alpha")["project_id"]
    old_b = _begin(store, "sb", b, name="beta")["project_id"]
    (tmp_path / "sessions" / ".identity" / "key").unlink()
    return store, a, b, old_a, old_b


def _leases(tmp_path):
    return LeaseStore(tmp_path / "leases")


def test_recovery_restores_identity_names_and_new_sessions(tmp_path, lost):
    store, a, b, old_a, old_b = lost
    with pytest.raises(ProjectKeyMissing):
        _begin(store, "blocked", a)

    report = recover_project_ids(store.root, leases=_leases(tmp_path))

    assert set(report.mapped) == {old_a, old_b} and report.unmapped == []
    meta = store.load_meta("sa")
    assert meta["project_id"] == report.mapped[old_a]
    assert meta["previous_project_ids"] == [old_a]
    assert store.resolve("alpha", workspace=a) == "sa"
    assert _begin(store, "sa2", a)["project_id"] == meta["project_id"]  # new sessions join it


def test_a_moved_or_replaced_repository_stays_unmapped(tmp_path, lost):
    store, a, b, old_a, old_b = lost
    shutil.rmtree(b)
    _repo(b, history="someone else's")  # same path, a different repository

    report = recover_project_ids(store.root, leases=_leases(tmp_path))

    assert report.unmapped == [old_b] and old_a in report.mapped
    assert store.load_meta("sb")["project_id"] == old_b
    assert store.resolve("sb") == "sb"  # still reachable by full id


def test_a_different_history_in_the_same_directory_stays_unmapped(tmp_path, lost):
    """The directory (and its inode) survive; the repository inside is another."""
    store, a, b, old_a, old_b = lost
    inode = os.stat(b).st_ino
    shutil.rmtree(b / ".git")
    (b / "README").unlink()
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_AUTHOR_NAME": "u",
           "GIT_AUTHOR_EMAIL": "u@u", "GIT_COMMITTER_NAME": "u", "GIT_COMMITTER_EMAIL": "u@u"}
    subprocess.run(["git", "init", "-q", str(b)], check=True, capture_output=True, env=env)
    (b / "OTHER").write_text("unrelated\n")
    subprocess.run(["git", "-C", str(b), "add", "OTHER"], check=True, capture_output=True, env=env)
    subprocess.run(["git", "-C", str(b), "commit", "-qm", "unrelated"], check=True,
                   capture_output=True, env=env)
    assert os.stat(b).st_ino == inode

    report = recover_project_ids(store.root, leases=_leases(tmp_path))

    assert report.unmapped == [old_b] and old_a in report.mapped


def test_recovery_refuses_while_a_session_may_be_running(tmp_path, lost):
    store, a, *_ = lost
    leases = _leases(tmp_path)
    leases.acquire(a, "sa", "mutating")  # owned by this live process

    with pytest.raises(RecoveryRefused, match="may still be running"):
        recover_project_ids(store.root, leases=leases)
    assert not (store.root / ".identity" / "key").exists()


def test_an_interrupted_recovery_resumes_without_mixing_keys(tmp_path, lost, monkeypatch):
    store, a, b, old_a, old_b = lost
    real_apply = recovery._apply

    def crash_after_first_session(root, plan):
        first = next(iter(plan["sessions"]))
        real_apply(root, {**plan, "sessions": {first: plan["sessions"][first]}, "mapping": {}})
        raise KeyboardInterrupt("crash mid-apply")

    monkeypatch.setattr(recovery, "_apply", crash_after_first_session)
    with pytest.raises(KeyboardInterrupt):
        recover_project_ids(store.root, leases=_leases(tmp_path))
    monkeypatch.undo()
    assert (store.root / ".identity" / "recovery.json").exists()

    report = recover_project_ids(store.root, leases=_leases(tmp_path))

    assert report.resumed is True
    ids = {store.load_meta("sa")["project_id"], store.load_meta("sb")["project_id"]}
    assert ids == set(report.mapped.values())
    assert store.resolve("beta", workspace=b) == "sb"
    assert not (store.root / ".identity" / "recovery.json").exists()


def test_a_crash_after_publishing_the_key_still_finishes(tmp_path, lost, monkeypatch):
    store, a, *_ = lost
    real_write = recovery.write_document
    calls = {"n": 0}

    def crash_on_published(path, doc):
        if doc.get("state") == "published" and path.name == "recovery.json":
            calls["n"] += 1
            raise KeyboardInterrupt("crash after os.replace(key)")
        return real_write(path, doc)

    monkeypatch.setattr(recovery, "write_document", crash_on_published)
    with pytest.raises(KeyboardInterrupt):
        recover_project_ids(store.root, leases=_leases(tmp_path))
    monkeypatch.undo()

    report = recover_project_ids(store.root, leases=_leases(tmp_path))
    assert report.resumed and store.resolve("alpha", workspace=a) == "sa"


def test_repeating_a_finished_recovery_is_a_noop(tmp_path, lost):
    store, *_ = lost
    recover_project_ids(store.root, leases=_leases(tmp_path))
    before = json.dumps(store.load_meta("sa"), sort_keys=True)

    assert recover_project_ids(store.root, leases=_leases(tmp_path)).noop is True
    assert json.dumps(store.load_meta("sa"), sort_keys=True) == before


def test_the_doctor_command_reports_and_refuses(tmp_path, lost, monkeypatch, capsys):
    import garuda.interfaces.main as main

    store, *_ = lost
    monkeypatch.setenv("GARUDA_SESSIONS_DIR", str(store.root))
    monkeypatch.setenv("GARUDA_LEASES_DIR", str(tmp_path / "leases"))
    args = main.build_parser().parse_args(["doctor", "--recover-project-ids"])

    assert main.run_doctor(args) == 0
    assert "Recovered 2 project(s)" in capsys.readouterr().out
    assert main.run_doctor(args) == 0
    assert "nothing to recover" in capsys.readouterr().out
