"""Session worktrees and safe integration on real repositories (#157, plan B.5)."""

import hashlib
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from garuda.core.events import EventStore
from garuda.core.loop import DefaultAgent
from garuda.core.permissions import PermissionEngine
from garuda.core.sessions import SessionStore
from garuda.model.protocol import ModelResponse
from garuda.model.script_model import ScriptModel
from garuda.tools import tools_for_names
from garuda.types import AgentConfig, ToolCall
from garuda.workspace import snapshot_proto as snap
from garuda.workspace import worktrees as wt
from garuda.workspace.lease import LeaseStore
from garuda.workspace.worktrees import WorktreeError

IMAGE = "python:3.12-slim"


def sh(repo: Path, *args: str) -> str:
    env = {**os.environ, **snap.IDENTITY, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True, env=env
    ).stdout.strip()


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("GARUDA_GLOBAL_SETTINGS", str(tmp_path / "home" / "settings.yaml"))
    monkeypatch.setenv("GARUDA_LEASES_DIR", str(tmp_path / "leases"))
    monkeypatch.setenv("GARUDA_SESSIONS_DIR", str(tmp_path / "sessions"))


@pytest.fixture
def repo(tmp_path):
    path = tmp_path / "repo"
    path.mkdir()
    sh(path, "init", "-q", "-b", "main")
    (path / "app.txt").write_text("one\n")
    sh(path, "add", ".")
    sh(path, "commit", "-qm", "base")
    return path


def _checkout_state(repo: Path) -> tuple:
    return (
        sh(repo, "rev-parse", "HEAD"),
        sh(repo, "symbolic-ref", "HEAD"),
        hashlib.sha256((repo / ".git" / "index").read_bytes()).hexdigest(),
        sh(repo, "status", "--porcelain=v1", "--ignored"),
        sh(repo, "for-each-ref", "--format=%(refname) %(objectname)", "refs/heads"),
    )


def _passing(directory, command, image, timeout):
    return {"command": command, "exit_code": 0, "passed": True}


# --- isolation --------------------------------------------------------------


def test_shared_is_the_workspace_itself(repo):
    plan = wt.prepare_workspace(repo, "s1", "shared")
    assert (plan.isolation, plan.path) == ("shared", str(repo.resolve()))
    assert sh(repo, "branch", "--list", "garuda/*") == ""


def test_a_worktree_gets_its_own_branch_and_does_not_inherit_uncommitted_edits(repo):
    (repo / "app.txt").write_text("uncommitted\n")
    (repo / "scratch.txt").write_text("untracked\n")

    plan = wt.prepare_workspace(repo, "s1", "worktree")

    path = Path(plan.path)
    assert plan.isolation == "worktree" and plan.branch == "garuda/s1"
    assert path.is_relative_to(wt.worktrees_root())
    assert sh(path, "symbolic-ref", "--short", "HEAD") == "garuda/s1"
    assert (path / "app.txt").read_text() == "one\n" and not (path / "scratch.txt").exists()
    assert plan.dirty_source and plan.dirty_fingerprint
    assert plan.source_head == sh(repo, "rev-parse", "HEAD")
    assert (repo / "app.txt").read_text() == "uncommitted\n"  # source untouched


def test_the_session_branch_has_one_owner(repo):
    wt.prepare_workspace(repo, "s1", "worktree")
    with pytest.raises(WorktreeError) as caught:
        wt.prepare_workspace(repo, "s1", "worktree")
    assert caught.value.code == "worktree.branch_taken"


def test_auto_stays_in_place_until_someone_else_is_editing(repo):
    assert wt.prepare_workspace(repo, "s1", "auto").isolation == "shared"
    LeaseStore().acquire(repo, "other", "mutating")
    assert wt.prepare_workspace(repo, "s2", "auto").isolation == "worktree"


@pytest.mark.parametrize("mode", ["worktree", "auto"])
def test_refusals(tmp_path, repo, mode):
    empty = tmp_path / "empty"
    empty.mkdir()
    sh(empty, "init", "-q")
    with pytest.raises(WorktreeError) as caught:
        wt.prepare_workspace(empty, "s1", "worktree")
    assert caught.value.code == "worktree.no_commit"
    with pytest.raises(WorktreeError):
        wt.prepare_workspace(repo, "s1", "elsewhere")


def test_discarding_an_unused_worktree_removes_branch_and_directory(repo):
    plan = wt.prepare_workspace(repo, "s1", "worktree").meta()
    wt.discard_worktree(plan)
    assert not Path(plan["worktree"]).exists()
    assert sh(repo, "branch", "--list", "garuda/*") == ""


# --- integration -------------------------------------------------------------


def _session(repo, session_id="s1"):
    plan = wt.prepare_workspace(repo, session_id, "worktree")
    return {**plan.meta(), "session_id": session_id}, Path(plan.path)


def test_merge_publishes_only_the_integration_ref(repo, monkeypatch):
    meta, tree = _session(repo)
    (tree / "app.txt").write_text("two\n")  # uncommitted in the worktree
    (tree / "new.txt").write_text("new\n")
    (repo / "other.txt").write_text("landed on main meanwhile\n")
    sh(repo, "add", "other.txt")
    sh(repo, "commit", "-qm", "main moves")
    before = _checkout_state(repo)
    monkeypatch.setattr(wt, "_run_check", _passing)

    result = wt.merge_session(meta, checks=["true"], image=IMAGE)

    assert _checkout_state(repo) == before
    assert sh(repo, "rev-parse", "refs/garuda/integration/s1") == result.commit
    assert result.apply_command == f"git -C {repo.resolve()} merge --ff-only {result.commit}"
    assert sh(repo, "show", f"{result.commit}:app.txt") == "two"
    assert sh(repo, "show", f"{result.commit}:other.txt") == "landed on main meanwhile"
    sh(repo, "merge", "-q", "--ff-only", result.commit)  # the printed command applies cleanly
    assert (repo / "new.txt").read_text() == "new\n"


def test_merge_refuses_without_a_check_or_without_docker(repo, monkeypatch):
    meta, tree = _session(repo)
    (tree / "app.txt").write_text("two\n")
    with pytest.raises(WorktreeError) as caught:
        wt.merge_session(meta, checks=[], image=IMAGE)
    assert caught.value.code == "integration.no_trusted_check"

    monkeypatch.setattr(wt.shutil, "which", lambda _name: None)
    with pytest.raises(WorktreeError) as caught:
        wt.merge_session(meta, checks=["true"], image=IMAGE)
    assert caught.value.code == "integration.no_confined_checker"
    assert sh(repo, "for-each-ref", "refs/garuda") == ""


def test_merge_refuses_conflicts_failed_checks_and_tree_changes(repo, monkeypatch):
    meta, tree = _session(repo)
    (tree / "app.txt").write_text("session\n")
    (repo / "app.txt").write_text("main\n")
    sh(repo, "commit", "-qam", "conflict")
    monkeypatch.setattr(wt, "_run_check", _passing)
    with pytest.raises(WorktreeError) as caught:
        wt.merge_session(meta, checks=["true"], image=IMAGE)
    assert caught.value.code == "integration.conflicts"

    meta, tree = _session(repo, "s2")
    (tree / "b.txt").write_text("b\n")
    monkeypatch.setattr(wt, "_run_check", lambda *a: {"command": "x", "passed": False})
    with pytest.raises(WorktreeError) as caught:
        wt.merge_session(meta, checks=["false"], image=IMAGE)
    assert caught.value.code == "integration.check_failed"

    def tamper(directory, command, image, timeout):
        (directory / "planted.txt").write_text("x")
        return _passing(directory, command, image, timeout)

    monkeypatch.setattr(wt, "_run_check", tamper)
    with pytest.raises(WorktreeError) as caught:
        wt.merge_session(meta, checks=["true"], image=IMAGE)
    assert caught.value.code == "integration.check_changed_tree"
    assert sh(repo, "for-each-ref", "refs/garuda") == ""


def test_a_second_publication_for_the_session_is_refused(repo, monkeypatch):
    meta, tree = _session(repo)
    (tree / "b.txt").write_text("b\n")
    monkeypatch.setattr(wt, "_run_check", _passing)
    wt.merge_session(meta, checks=["true"], image=IMAGE)
    (tree / "c.txt").write_text("c\n")
    with pytest.raises(WorktreeError) as caught:
        wt.merge_session(meta, checks=["true"], image=IMAGE)
    assert caught.value.code == "integration.ref_moved"


def test_removal_refuses_unpublished_work(repo, monkeypatch):
    meta, tree = _session(repo)
    (tree / "b.txt").write_text("b\n")
    with pytest.raises(WorktreeError) as caught:
        wt.remove_worktree(meta)
    assert caught.value.code == "worktree.unmerged" and tree.exists()

    monkeypatch.setattr(wt, "_run_check", _passing)
    wt.merge_session(meta, checks=["true"], image=IMAGE)
    (tree / "c.txt").write_text("after publication\n")
    with pytest.raises(WorktreeError):
        wt.remove_worktree(meta)
    (tree / "c.txt").unlink()
    wt.remove_worktree(meta)  # exactly what was published
    assert not tree.exists()


def _docker_image_present() -> bool:
    if shutil.which("docker") is None:
        return False
    probe = subprocess.run(
        ["docker", "run", "--rm", "--pull", "never", "--network", "none", IMAGE, "true"],
        capture_output=True,
    )
    return probe.returncode == 0


@pytest.mark.skipif(not _docker_image_present(), reason=f"needs Docker with {IMAGE} pulled")
def test_checks_really_run_in_docker_against_the_merged_tree(repo):
    meta, tree = _session(repo)
    (tree / "app.txt").write_text("two\n")
    result = wt.merge_session(meta, checks=["grep -q two app.txt"], image=IMAGE)
    assert result.checks[0]["passed"]
    meta, tree = _session(repo, "s2")
    (tree / "b.txt").write_text("b\n")
    with pytest.raises(WorktreeError) as caught:  # the tree is mounted read-only
        wt.merge_session(meta, checks=["touch /src/x"], image=IMAGE)
    assert caught.value.code == "integration.check_failed"
    assert sh(repo, "for-each-ref", "--format=%(refname)", "refs/garuda") == (
        "refs/garuda/integration/s1"
    )


# --- through the runner --------------------------------------------------------


async def _run(workspace, session_id, *, isolation, resume=None):
    from garuda.interfaces.runner import run_agent_task

    model = ScriptModel([
        ModelResponse(content=None, tool_calls=[ToolCall(
            id="1", name="bash", arguments={"command": f"echo {session_id} > made.txt"})]),
        ModelResponse(content=None, tool_calls=[ToolCall(
            id="2", name="task_complete", arguments={"summary": "ok"})]),
    ])
    return await run_agent_task(
        task="do the work",
        model=model,
        agent=DefaultAgent(),
        tools=tools_for_names(["bash", "task_complete"]),
        config=AgentConfig(max_turns=5, enable_verifier=False, permission_mode="yolo"),
        permissions=PermissionEngine(mode="yolo"),
        workspace=str(workspace),
        events=EventStore(session_id=session_id),
        isolation=isolation,
        resume=resume,
    )


async def test_a_worktree_run_edits_and_is_attributed_in_its_worktree(repo):
    result = await _run(repo, "wtrun", isolation="worktree")

    assert result.success, result.final_message
    meta = SessionStore().load_meta("wtrun")
    tree = Path(meta["worktree"])
    assert meta["isolation"] == "worktree" and meta["branch"] == "garuda/wtrun"
    assert (tree / "made.txt").read_text() == "wtrun\n"
    assert not (repo / "made.txt").exists()
    assert result.metadata["workspace_delta"]["changed"] == ["made.txt"]
    assert LeaseStore().holders_of(tree) == []  # released


async def test_a_refused_launch_leaves_no_worktree_behind(repo, monkeypatch):
    from garuda.interfaces.run_guard import WorkspaceLeaseGuard
    from garuda.runtime.capacity import CapacityUnavailable

    def full(self):
        raise CapacityUnavailable("native is at capacity")

    monkeypatch.setattr(WorkspaceLeaseGuard, "acquire", full)
    with pytest.raises(CapacityUnavailable):
        await _run(repo, "refused", isolation="worktree")
    assert sh(repo, "worktree", "list", "--porcelain").count("worktree ") == 1
    assert sh(repo, "branch", "--list", "garuda/*") == ""


async def test_a_resumed_worktree_session_returns_to_its_worktree(repo):
    await _run(repo, "first", isolation="worktree")
    tree = Path(SessionStore().load_meta("first")["worktree"])
    result = await _run(repo, "second", isolation="shared", resume="first")

    assert result.success, result.final_message
    assert SessionStore().load_meta("second")["worktree"] == str(tree)
    assert (tree / "made.txt").read_text() == "second\n"
    assert not (repo / "made.txt").exists()
    with pytest.raises(WorktreeError):
        await _run(repo, "third", isolation="worktree", resume="first")


async def test_the_cli_refuses_a_merge_without_a_check(repo, capsys):
    from garuda.interfaces.main import build_parser, run_sessions

    await _run(repo, "clirun", isolation="worktree")
    parse = build_parser().parse_args
    assert run_sessions(parse(["sessions", "merge", "clirun", "--workspace", str(repo)])) == 2
    assert "integration.no_trusted_check" in capsys.readouterr().err
    remove = parse(["sessions", "remove-worktree", "clirun", "--workspace", str(repo)])
    assert run_sessions(remove) == 2  # unpublished
    assert Path(SessionStore().load_meta("clirun")["worktree"]).exists()


@pytest.mark.parametrize("into", ["--oops", "no-such-branch", "bad..name"])
def test_merge_needs_an_existing_destination_branch(repo, monkeypatch, into):
    meta, tree = _session(repo)
    (tree / "b.txt").write_text("b\n")
    monkeypatch.setattr(wt, "_run_check", _passing)
    with pytest.raises(WorktreeError) as caught:
        wt.merge_session(meta, checks=["true"], image=IMAGE, destination=into)
    assert caught.value.code == "integration.no_destination"


def test_a_detached_head_needs_an_explicit_destination(repo, monkeypatch):
    meta, tree = _session(repo)
    (tree / "b.txt").write_text("b\n")
    sh(repo, "checkout", "-q", "--detach")
    monkeypatch.setattr(wt, "_run_check", _passing)
    with pytest.raises(WorktreeError) as caught:
        wt.merge_session(meta, checks=["true"], image=IMAGE)
    assert caught.value.code == "integration.no_destination"
    assert wt.merge_session(meta, checks=["true"], image=IMAGE, destination="main").destination == "main"
