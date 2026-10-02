"""Opaque project ids and unique per-project session names (#157, plan task B.1)."""

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from garuda.core import project_identity as pid
from garuda.core.project_identity import ProjectKeyMissing, SessionNameTaken
from garuda.core.sessions import SessionStore

ROOT = Path(__file__).resolve().parents[1]


def _git_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull}
    run = lambda *a: subprocess.run(["git", "-C", str(path), *a], check=True, capture_output=True, env=env)  # noqa: E731
    run("init", "-q")
    (path / "f").write_text("x")
    run("add", "f")
    run("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init")
    return path


def _begin(store: SessionStore, session_id: str, workspace: Path, task="fix the login bug", **kw):
    store.begin(session_id, task=task, model="m", agent="a", workspace=str(workspace), **kw)
    return store.load_meta(session_id)


def test_concurrent_processes_get_distinct_names_and_one_key(tmp_path):
    repo = _git_repo(tmp_path / "repo")
    store_root = tmp_path / "sessions"
    code = textwrap.dedent(
        """
        import sys
        from garuda.core.sessions import SessionStore
        s = SessionStore(sys.argv[1])
        s.begin(sys.argv[3], task="fix the login bug", model="m", agent="a", workspace=sys.argv[2])
        m = s.load_meta(sys.argv[3])
        print(m["project_id"], m["name"])
        """
    )
    env = dict(os.environ, PYTHONPATH=str(ROOT))
    procs = [
        subprocess.Popen([sys.executable, "-c", code, str(store_root), str(repo), f"s{i}"],
                         env=env, stdout=subprocess.PIPE, text=True)
        for i in range(8)
    ]
    rows = [p.communicate(timeout=120)[0].split() for p in procs]

    assert len({row[0] for row in rows}) == 1  # one project id, hence one key
    names = [row[1] for row in rows]
    assert len(set(names)) == 8
    assert "fix-the-login-bug" in names
    assert len(list((store_root / ".identity").glob("key"))) == 1


def test_worktrees_and_symlinks_share_a_project_id(tmp_path):
    repo = _git_repo(tmp_path / "repo")
    worktree = tmp_path / "repo-feature"
    subprocess.run(["git", "-C", str(repo), "worktree", "add", "-q", str(worktree)], check=True,
                   capture_output=True, env={**os.environ, "GIT_CONFIG_GLOBAL": os.devnull})
    alias = tmp_path / "alias"
    alias.symlink_to(repo)
    store = SessionStore(tmp_path / "sessions")

    ids = {_begin(store, f"s{i}", ws)["project_id"] for i, ws in enumerate([repo, worktree, alias])}
    other = _begin(store, "other", _git_repo(tmp_path / "other"))["project_id"]

    assert len(ids) == 1
    assert other not in ids


def test_ids_reveal_no_path_and_the_key_is_private(tmp_path):
    repo = _git_repo(tmp_path / "repo")
    store = SessionStore(tmp_path / "sessions")
    meta = _begin(store, "s1", repo)

    assert meta["project_id"].startswith("p1_") and len(meta["project_id"]) == 35
    assert str(repo) not in meta["project_id"]
    key = tmp_path / "sessions" / ".identity" / "key"
    assert oct(key.stat().st_mode & 0o777) == "0o600"
    assert key.read_text() not in json.dumps(meta)


def test_a_lost_key_refuses_instead_of_splitting_projects(tmp_path):
    repo = _git_repo(tmp_path / "repo")
    store = SessionStore(tmp_path / "sessions")
    _begin(store, "s1", repo)
    (tmp_path / "sessions" / ".identity" / "key").unlink()

    with pytest.raises(ProjectKeyMissing, match="recover-project-ids"):
        _begin(store, "s2", repo)
    assert not (tmp_path / "sessions" / ".identity" / "key").exists()


def test_names_are_unique_per_project_and_resolve_within_it(tmp_path):
    a, b = _git_repo(tmp_path / "a"), _git_repo(tmp_path / "b")
    store = SessionStore(tmp_path / "sessions")
    _begin(store, "sa", a, name="review")
    _begin(store, "sb", b, name="review")  # same name, another project: fine

    with pytest.raises(SessionNameTaken):
        _begin(store, "sa2", a, name="review")
    assert store.resolve("review", workspace=a) == "sa"
    assert store.resolve("review", workspace=b) == "sb"
    with pytest.raises(FileNotFoundError):
        store.resolve("review")  # no project: names are not global


def test_prefixes_and_latest_stay_in_the_project(tmp_path):
    a, b = _git_repo(tmp_path / "a"), _git_repo(tmp_path / "b")
    store = SessionStore(tmp_path / "sessions")
    _begin(store, "abc111", a)
    _begin(store, "abc222", b)

    assert store.resolve("abc", workspace=a) == "abc111"
    assert store.resolve("latest", workspace=a) == "abc111"
    with pytest.raises(ValueError, match="Ambiguous"):
        store.resolve("abc", workspace=a, all_projects=True)
    assert store.resolve("abc222") == "abc222"  # a full id always resolves


def test_a_second_begin_keeps_the_identity(tmp_path):
    repo = _git_repo(tmp_path / "repo")
    store = SessionStore(tmp_path / "sessions")
    first = _begin(store, "s1", repo, name="keep-me")
    again = _begin(store, "s1", repo, task="another task")

    assert (again["name"], again["project_id"]) == ("keep-me", first["project_id"])


def test_legacy_sessions_still_resolve_by_path(tmp_path):
    repo = _git_repo(tmp_path / "repo")
    store = SessionStore(tmp_path / "sessions")
    legacy = tmp_path / "sessions" / "old1"
    legacy.mkdir(parents=True)
    (legacy / "meta.json").write_text(json.dumps(
        {"session_id": "old1", "workspace": str(repo), "updated_at": "2026-01-01", "task": "t"}
    ))

    assert store.resolve("latest", workspace=repo) == "old1"
    assert store.resolve("old", workspace=repo) == "old1"


def test_name_slugs():
    assert pid.slugify("Fix the login bug on Safari now please") == "fix-the-login-bug-on"
    assert pid.slugify("!!!") == "session"
    with pytest.raises(pid.ProjectIdentityError):
        pid.validate_name("Bad Name")


def test_the_cli_names_a_session_and_lists_it(tmp_path, monkeypatch, capsys):
    import garuda.interfaces.main as main

    args = main.build_parser().parse_args(["run", "-t", "x", "--name", "my-task"])
    assert args.name == "my-task"
    monkeypatch.setenv("GARUDA_SESSIONS_DIR", str(tmp_path / "sessions"))
    _begin(SessionStore(), "s1", _git_repo(tmp_path / "repo"), name="my-task")
    main.run_sessions(main.build_parser().parse_args(["sessions"]))
    assert "my-task" in capsys.readouterr().out
