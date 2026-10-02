"""``--resume latest`` resumes the current project's newest session (#149).

The session store is global, and ``latest`` used to mean the newest session
anywhere: working in repository A after B, ``--resume latest`` in A resumed B's
conversation. Sessions also recorded ``workspace: "."``, so they could not be told
apart. These runs go through the SDK with real workspaces and a real session store.
"""

import subprocess
from pathlib import Path

import pytest

from garuda.core.sessions import SessionStore, project_root
from garuda.model.protocol import ModelResponse
from garuda.model.script_model import ScriptModel
from garuda.sdk.software_agent import SoftwareAgent
from garuda.types import ToolCall


def _done() -> ScriptModel:
    return ScriptModel(
        [
            ModelResponse(
                content=None,
                tool_calls=[
                    ToolCall(
                        id="d",
                        name="task_complete",
                        arguments={"summary": "A fully detailed completion summary of the work done."},
                    )
                ],
            )
        ]
    )


def _git_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    (path / "README").write_text("x")
    subprocess.run(["git", "-C", str(path), "add", "README"], check=True)
    subprocess.run(
        ["git", "-C", str(path), "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"],
        check=True,
    )
    return path


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("GARUDA_SESSIONS_DIR", str(tmp_path / "sessions"))
    monkeypatch.setenv("GARUDA_GLOBAL_SETTINGS", str(tmp_path / "home" / "settings.yaml"))
    return SessionStore()


async def _run(workspace: Path, task: str, **resume) -> str:
    agent = SoftwareAgent(workspace=str(workspace), model=_done(), agent="explore")
    result = await agent.run(task, **resume)
    return result.metadata.get("session_id") or SessionStore().list_sessions(limit=1)[0]["session_id"]


def _resumed_from(store: SessionStore) -> str | None:
    return store.list_sessions(limit=1)[0].get("resumed_from")


async def test_latest_resumes_this_projects_session_not_a_newer_one_elsewhere(tmp_path, store):
    a, b = _git_repo(tmp_path / "a"), _git_repo(tmp_path / "b")
    session_a = await _run(a, "work in a")
    session_b = await _run(b, "work in b")  # newest overall
    assert session_a != session_b

    await _run(a, "continue", resume="latest")

    assert _resumed_from(store) == session_a


async def test_all_projects_still_means_the_newest_anywhere(tmp_path, store):
    a, b = _git_repo(tmp_path / "a"), _git_repo(tmp_path / "b")
    await _run(a, "work in a")
    session_b = await _run(b, "work in b")

    await _run(a, "continue", resume="latest", resume_all_projects=True)

    assert _resumed_from(store) == session_b


async def test_sessions_record_an_absolute_workspace(tmp_path, store):
    a = _git_repo(tmp_path / "a")
    await _run(a, "work in a")

    assert store.list_sessions(limit=1)[0]["workspace"] == str(a.resolve())


def test_a_linked_worktree_and_a_symlink_are_the_same_project(tmp_path):
    a = _git_repo(tmp_path / "a")
    worktree = tmp_path / "a-feature"
    subprocess.run(["git", "-C", str(a), "worktree", "add", "-q", str(worktree)], check=True)
    alias = tmp_path / "alias"
    alias.symlink_to(a)

    assert project_root(worktree) == project_root(a) == project_root(alias) == str(a.resolve())
    assert project_root(a / "README") == str(a.resolve())


def test_a_project_with_no_session_says_so_and_names_the_flag(tmp_path, store):
    other = tmp_path / "other"
    other.mkdir()
    store.begin("s1", task="t", model="m", agent="a", workspace=str(other.resolve()))
    lonely = tmp_path / "lonely"
    lonely.mkdir()

    with pytest.raises(FileNotFoundError, match="--all-projects"):
        store.resolve("latest", workspace=lonely)


def test_legacy_sessions_without_an_absolute_workspace_are_skipped(tmp_path, store):
    here = tmp_path / "here"
    here.mkdir()
    store.begin("old", task="t", model="m", agent="a", workspace=".")

    with pytest.raises(FileNotFoundError):
        store.resolve("latest", workspace=here)
    assert store.resolve("latest") == "old"  # no workspace given: unchanged
