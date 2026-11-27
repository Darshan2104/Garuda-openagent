"""Worktree isolation and checkpoints for configured flows."""

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from garuda.config import garuda_yaml as gy
from garuda.core.sessions import SessionStore
from garuda.flows import checkpoints as cp
from garuda.flows import engine
from garuda.flows.engine import StepResult
from garuda.workspace.lease import LeaseStore
from garuda.workspace.worktrees import prepare_workspace, worktrees_root
from tests.test_flows import _block, _git


@pytest.fixture
def repo(tmp_path):
    path = tmp_path / "repo"
    path.mkdir()
    _git(path, "init", "-q", "-b", "main")
    (path / "a.txt").write_text("a\n")
    (path / ".gitignore").write_text("*.log\n")
    _git(path, "add", ".")
    _git(path, "commit", "-qm", "base")
    return path


def _out(path, *args) -> str:
    return subprocess.run(["git", "-C", str(path), *args], check=True,
                          capture_output=True).stdout.decode().strip()


def _state(path) -> tuple:
    """HEAD, branch, index and status: what a worktree flow must never change here."""
    return (_out(path, "rev-parse", "HEAD"), _out(path, "symbolic-ref", "HEAD"),
            _out(path, "ls-files", "-s"), _out(path, "status", "--porcelain=v1", "-uall"),
            (path / "a.txt").read_text())


FLOW = ("version: 1\nroles:\n  planner: {harness: native, write_policy: no-edits}\n"
        "  coder: {harness: native}\n"
        "flows:\n  wt:\n    isolation: worktree\n    steps:\n"
        "      - {id: plan, role: planner, outputs: [plan]}\n"
        "      - {id: build, role: coder, inputs: [plan]}\n"
        "      - {id: polish, role: coder}\n"
        "  plain:\n    steps:\n      - {id: build, role: coder}\n")


def _config(text=FLOW):
    gy.user_path().parent.mkdir(parents=True, exist_ok=True)
    gy.user_path().write_text(text)


class Steps:
    """A stub launcher: ``plan`` writes nothing, ``build`` edits, ``polish`` changes nothing."""

    def __init__(self, fail=()):
        self.launches = []
        self.fail = set(fail)

    async def __call__(self, launch):
        self.launches.append(launch)
        root = Path(launch.workspace)
        if (launch.step_id, launch.attempt) in self.fail:
            return StepResult(str(uuid.uuid4()), False, "")
        if launch.step_id == "plan":
            return StepResult(str(uuid.uuid4()), True, _block("plan", "edit a.txt"))
        if launch.step_id == "build":
            (root / "a.txt").write_text("b\n")
            (root / "new.txt").write_text("untracked\n")
            (root / "debug.log").write_text("ignored\n")
        return StepResult(str(uuid.uuid4()), True, "done")


def _run(monkeypatch, capsys, *args):
    from tests.test_runtime_cli import _main

    return _main(monkeypatch, capsys, *args)


def _flow_id(out) -> str:
    return json.loads(next(line for line in out.splitlines()
                           if line.startswith("{")))["flow_session"]


def test_a_worktree_flow_leaves_the_checkout_untouched_until_merge(repo, monkeypatch, capsys):
    _config()
    before = _state(repo)
    steps = Steps()
    monkeypatch.setattr("garuda.flows.launch.launch_step", steps)

    code, out = _run(monkeypatch, capsys, "flow", "run", "wt", "-t", "ship it",
                     "--workspace", str(repo))

    assert code == 0, out
    flow_id = _flow_id(out)
    worktree = Path(steps.launches[0].workspace)
    assert worktree != repo and worktrees_root().resolve() in worktree.parents
    assert {launch.workspace for launch in steps.launches} == {str(worktree)}
    assert (worktree / "a.txt").read_text() == "b\n"
    assert _state(repo) == before  # nothing reached the user's checkout
    assert f"garuda sessions merge {flow_id[:8]}" in out
    store = SessionStore()
    meta = store.load_meta(flow_id)
    assert meta["isolation"] == "worktree" and meta["worktree"] == str(worktree)
    assert meta["flow_execution_workspace"]["source"] == str(repo.resolve())
    assert LeaseStore().holders_of(worktree) == [] and LeaseStore().holders_of(repo) == []
    # The journal keeps exactly its two entries per attempt; checkpoints live apart.
    assert [e["event"] for e in engine.journal(store, flow_id)] == ["intent", "receipt"] * 3

    monkeypatch.setattr("garuda.workspace.worktrees._run_check", lambda directory, command, image,
                        timeout: {"command": command, "exit_code": 0, "passed": True})
    code, out = _run(monkeypatch, capsys, "sessions", "merge", flow_id, "--check", "true",
                     "--workspace", str(repo))
    assert code == 0, out
    assert _state(repo) == before  # the merge step publishes a ref, never the checkout
    commit = _out(repo, "rev-parse", f"refs/garuda/integration/{flow_id}")
    _git(repo, "merge", "--ff-only", commit)
    assert (repo / "a.txt").read_text() == "b\n" and (repo / "new.txt").is_file()
    assert not (repo / "debug.log").exists()


def test_each_attempt_is_checkpointed_without_touching_index_or_head(repo, monkeypatch, capsys):
    _config()
    before = _state(repo)
    monkeypatch.setattr("garuda.flows.launch.launch_step", Steps())
    code, out = _run(monkeypatch, capsys, "flow", "run", "wt", "-t", "ship it",
                     "--workspace", str(repo))
    assert code == 0, out
    flow_id = _flow_id(out)
    store = SessionStore()
    meta = store.load_meta(flow_id)
    worktree = Path(meta["worktree"])
    rows = cp.listing(engine.flow_dir(store, flow_id))
    # plan (the first, unchanged from HEAD) and build; polish changed nothing: no duplicate.
    assert [(r["n"], r["step"], r["attempt"]) for r in rows] == [(1, "plan", 1), (2, "build", 1)]
    assert [r["ref"] for r in rows] == [f"refs/garuda/{flow_id}/1", f"refs/garuda/{flow_id}/2"]
    for row in rows:
        assert _out(repo, "rev-parse", row["ref"]) == row["commit"]
        assert set(row) >= {"ref", "commit", "step", "attempt", "label", "at"}
    files = _out(repo, "ls-tree", "-r", "--name-only", rows[1]["commit"]).splitlines()
    assert "new.txt" in files and "debug.log" not in files  # untracked in, ignored out
    assert _out(repo, "show", f"{rows[1]['commit']}:a.txt") == "b"
    assert _state(repo) == before
    # The worktree's own HEAD and index are untouched too: new.txt stays untracked.
    assert _out(worktree, "rev-parse", "HEAD") == before[0]
    assert "new.txt" not in _out(worktree, "ls-files")
    assert "?? new.txt" in _out(worktree, "status", "--porcelain=v1")
    assert cp.checkpoint(engine.flow_dir(store, flow_id), worktree, "again") == rows[1]["ref"]
    assert len(cp.listing(engine.flow_dir(store, flow_id))) == 2

    code, out = _run(monkeypatch, capsys, "flow", "show", flow_id, "--workspace", str(repo))
    assert code == 0, out
    assert f"  worktree: {worktree}" in out
    assert "  checkpoint 1: plan attempt 1; " in out and "  checkpoint 2: build attempt 1; " in out
    code, out = _run(monkeypatch, capsys, "flow", "show", flow_id, "--json")
    assert [row["n"] for row in json.loads(out)["checkpoints"]] == [1, 2]


def test_rollback_restores_files_and_refuses_while_running(repo, monkeypatch, capsys):
    _config()
    monkeypatch.setattr("garuda.flows.launch.launch_step", Steps())
    code, out = _run(monkeypatch, capsys, "flow", "run", "wt", "-t", "ship it",
                     "--workspace", str(repo))
    assert code == 0, out
    flow_id = _flow_id(out)
    store = SessionStore()
    worktree = Path(store.load_meta(flow_id)["worktree"])
    (worktree / "later.txt").write_text("after the last checkpoint\n")

    lease = LeaseStore()
    lease.acquire(worktree, flow_id, "mutating")  # the flow still holds its worktree
    try:
        code, out = _run(monkeypatch, capsys, "flow", "rollback", flow_id, "--to", "1")
        assert code == 2 and "flow.running" in out
    finally:
        lease.release(worktree, flow_id)
    store.update_meta(flow_id, {"flow_state": "running"})
    code, out = _run(monkeypatch, capsys, "flow", "rollback", flow_id, "--to", "1")
    assert code == 2 and "flow.running" in out
    store.update_meta(flow_id, {"flow_state": "completed"})
    assert (worktree / "a.txt").read_text() == "b\n"

    code, out = _run(monkeypatch, capsys, "flow", "rollback", flow_id, "--to", "1")

    assert code == 0, out
    assert (worktree / "a.txt").read_text() == "a\n"
    assert not (worktree / "new.txt").exists() and not (worktree / "later.txt").exists()
    assert (worktree / "debug.log").read_text() == "ignored\n"  # ignored files are not state
    assert _out(worktree, "status", "--porcelain=v1") == ""
    rows = cp.listing(engine.flow_dir(store, flow_id))
    assert rows[-1]["label"] == "before rollback to 1" and rows[-1]["rolled_back_to"] == 1
    assert LeaseStore().holders_of(worktree) == []
    # The state before the rollback is itself a checkpoint: undo it.
    code, out = _run(monkeypatch, capsys, "flow", "rollback", flow_id, "--to", str(rows[-1]["n"]))
    assert code == 0, out
    assert (worktree / "later.txt").is_file() and (worktree / "a.txt").read_text() == "b\n"
    code, out = _run(monkeypatch, capsys, "flow", "rollback", flow_id, "--to", "99")
    assert code == 2 and "flow.checkpoint_unknown" in out


def test_rollback_refuses_a_shared_flow(repo, monkeypatch, capsys):
    _config()
    before = _state(repo)
    monkeypatch.setattr("garuda.flows.launch.launch_step", Steps())
    code, out = _run(monkeypatch, capsys, "flow", "run", "plain", "-t", "ship it",
                     "--workspace", str(repo))
    assert code == 0, out
    flow_id = _flow_id(out)
    meta = SessionStore().load_meta(flow_id)
    assert meta.get("isolation") is None and meta["workspace"] == str(repo.resolve())
    assert cp.listing(engine.flow_dir(SessionStore(), flow_id)) == []
    assert "refs/garuda/" not in _out(repo, "for-each-ref")
    code, out = _run(monkeypatch, capsys, "flow", "rollback", flow_id, "--to", "1")
    assert code == 2 and "flow.rollback_shared" in out
    assert _state(repo) != before  # a shared flow edits the checkout, as before


def test_cli_isolation_overrides_the_flow(repo, monkeypatch, capsys):
    _config()
    before = _state(repo)
    steps = Steps()
    monkeypatch.setattr("garuda.flows.launch.launch_step", steps)
    code, out = _run(monkeypatch, capsys, "flow", "run", "plain", "-t", "ship it",
                     "--workspace", str(repo), "--isolation", "worktree")
    assert code == 0, out
    assert _state(repo) == before and steps.launches[0].workspace != str(repo)
    code, out = _run(monkeypatch, capsys, "flow", "run", "wt", "-t", "ship it",
                     "--workspace", str(repo), "--isolation", "shared")
    assert code == 0, out
    assert steps.launches[-1].workspace == str(repo.resolve())


def test_a_resumed_worktree_flow_goes_back_to_its_worktree(repo, monkeypatch, capsys):
    _config()
    before = _state(repo)
    monkeypatch.setattr("garuda.flows.launch.launch_step", Steps(fail={("polish", 1)}))
    code, out = _run(monkeypatch, capsys, "flow", "run", "wt", "-t", "ship it",
                     "--workspace", str(repo))
    assert code == 3, out
    flow_id = _flow_id(out)
    worktree = SessionStore().load_meta(flow_id)["worktree"]
    steps = Steps()
    monkeypatch.setattr("garuda.flows.launch.launch_step", steps)
    code, out = _run(monkeypatch, capsys, "flow", "resume", flow_id)
    assert code == 0, out
    assert [(launch.step_id, launch.workspace) for launch in steps.launches] == [("polish", worktree)]
    assert _state(repo) == before
    assert len(os.listdir(Path(worktree).parent)) == 1  # no second worktree


def test_a_worktree_flow_needs_a_committed_repository(tmp_path, monkeypatch, capsys):
    _config()
    plain = tmp_path / "plain"
    plain.mkdir()
    monkeypatch.setattr("garuda.flows.launch.launch_step", Steps())
    code, out = _run(monkeypatch, capsys, "flow", "run", "wt", "-t", "ship it",
                     "--workspace", str(plain))
    assert code == 2 and "snapshot.not_git" in out
    assert not [m for m in SessionStore().list_sessions() if m.get("kind") == "flow"]


def _worktree_flow(repo):
    session = str(uuid.uuid4())
    plan = prepare_workspace(repo, session, "worktree")
    flow_dir = engine.flow_dir(SessionStore(), session)
    flow_dir.mkdir(parents=True)
    return session, Path(plan.path), flow_dir, plan


def test_pruning_keeps_the_first_and_the_last_checkpoint(repo):
    session, worktree, flow_dir, plan = _worktree_flow(repo)
    for n in range(1, 6):
        (worktree / "a.txt").write_text(f"{n}\n")
        cp.checkpoint(flow_dir, worktree, f"turn {n}", limit=3)
    rows = cp.listing(flow_dir)
    assert [(r["n"], r["pruned"]) for r in rows] == [
        (1, False), (2, True), (3, True), (4, False), (5, False)]
    refs = _out(repo, "for-each-ref", "--format=%(refname)", f"refs/garuda/{session}/")
    assert refs.splitlines() == [f"refs/garuda/{session}/{n}" for n in (1, 4, 5)]
    with pytest.raises(cp.CheckpointError, match="flow.checkpoint_pruned"):
        cp.rollback(flow_dir, worktree, 2)

    assert cp.prune_expired(repo, 30) == []  # fresh checkpoints are kept
    leases = LeaseStore()
    leases.acquire(worktree, session, "mutating")  # a running flow keeps its refs
    try:
        assert cp.prune_expired(repo, 30, now=10**11) == []
    finally:
        leases.release(worktree, session)
    assert cp.prune_expired(repo, 30, now=10**11) == [session]
    assert _out(repo, "for-each-ref", f"refs/garuda/{session}/") == ""


def test_removing_the_worktree_prunes_its_refs(repo):
    from garuda.workspace.worktrees import remove_worktree

    session, worktree, flow_dir, plan = _worktree_flow(repo)
    (worktree / "a.txt").write_text("x\n")
    cp.checkpoint(flow_dir, worktree, "one")
    other, other_tree, other_dir, _ = _worktree_flow(repo)
    cp.checkpoint(other_dir, other_tree, "kept")
    remove_worktree(plan.meta(), force=True)
    assert _out(repo, "for-each-ref", f"refs/garuda/{session}/") == ""
    assert _out(repo, "for-each-ref", "--format=%(refname)", f"refs/garuda/{other}/") == (
        f"refs/garuda/{other}/1")


def test_a_checkpoint_is_a_no_op_outside_a_garuda_worktree(repo, tmp_path):
    flow_dir = tmp_path / "flow"
    flow_dir.mkdir()
    assert cp.checkpoint(flow_dir, repo, "shared") is None
    assert not (flow_dir / cp.CHECKPOINTS).exists()


#: The digest of the ``pbr`` flow in ``tests.test_flows.CONFIG`` on origin/main:
#: a flow without ``isolation`` keeps the definition digest it had.
PBR_DIGEST = "d99958b4957748ead0c118962b54162cd4f50bdd89f7cfd0e5b24d3dfad785c4"


def test_a_flow_without_isolation_keeps_its_definition_digest():
    from garuda.core.acceptance_records import digest
    from tests.test_flows import CONFIG

    flow = gy.parse(CONFIG)["flows"]["pbr"]
    assert "isolation" not in flow
    assert digest(flow) == PBR_DIGEST
    with_key = gy.parse({**CONFIG, "flows": {"pbr": {**CONFIG["flows"]["pbr"],
                                                     "isolation": "worktree"}}})["flows"]["pbr"]
    assert with_key["isolation"] == "worktree" and digest(with_key) != PBR_DIGEST
    with pytest.raises(gy.GarudaConfigError, match="isolation"):
        gy.parse({**CONFIG, "flows": {"pbr": {**CONFIG["flows"]["pbr"], "isolation": "auto"}}})


async def test_a_worktree_review_packet_binds_the_same_candidate(repo):
    from tests.test_flow_review import APPROVE, PacketScript
    from tests.test_flow_review import _config as review_config

    session, worktree, _flow_dir, plan = _worktree_flow(repo)
    _flow_dir.rmdir()
    _flow_dir.parent.rmdir()
    resolved = gy.resolve(gy.parse(review_config(max_rounds=0)), None)
    info = worktree.stat()
    script = PacketScript([APPROVE])
    runner = engine.FlowRunner(
        SessionStore(), worktree, "pbr", resolved.config["flows"]["pbr"], resolved, task="t",
        launcher=script, flow_session=session,
        execution_workspace={"source": str(repo), "path": str(worktree),
                             "identity": [info.st_dev, info.st_ino]},
        worktree_plan=plan.meta())
    result = await runner.run()
    assert result.completed
    meta = SessionStore().load_meta(session)
    build = next(r for r in result.receipts if r["step"] == "build")
    candidate = meta["review_packet"]["candidate"]
    assert candidate["workspace_version"] == build["workspace_version_after"]
    assert (candidate["session_id"], candidate["attempt"]) == (build["session_id"], 1)
    assert meta["review_approvals"]["build"]["candidate"] == candidate
    assert cp.listing(engine.flow_dir(SessionStore(), session))  # checkpoints did not disturb it


@pytest.mark.skipif(sys.platform != "linux", reason="owned flow checks require Linux")
@pytest.mark.parametrize("check, passed", [("test -f new.txt", True), ("false", False)])
def test_a_checked_worktree_flow_runs_its_checks_in_the_worktree(
        repo, monkeypatch, capsys, check, passed):
    _config("version: 1\nroles:\n  coder: {harness: native}\n"
            "flows:\n  wt:\n    isolation: worktree\n    steps:\n      - {id: build, role: coder}\n")
    before = _state(repo)

    async def launcher(launch):
        assert launch.require_owned and launch.worktree_plan["isolation"] == "worktree"
        session = str(uuid.uuid4())
        lease = launch.capability._parent.snapshot_ownership()["lease"]
        info = Path(launch.workspace).stat()
        (Path(launch.workspace) / "new.txt").write_text("made in the worktree\n")
        closure = {"version": 1, "request_digest": "a" * 64, "session_id": session,
                   "parent_id": launch.flow_session, "step_id": launch.step_id,
                   "attempt": launch.attempt, "workspace_identity": [info.st_dev, info.st_ino],
                   "parent_owner": {key: lease[key] for key in ("pid", "identity", "pgid", "epoch")},
                   "process": {"cleanup": {"backend": "linux-subreaper", "echild": True},
                               "outcome": "exited", "supervisor": {}, "command": {}, "exit_code": 0}}
        return StepResult(session, True, "done", closure)

    monkeypatch.setattr("garuda.flows.launch.launch_step", launcher)
    code, out = _run(monkeypatch, capsys, "flow", "run", "wt", "-t", "ship it",
                     "--workspace", str(repo), "--check", check)
    assert code == 0, out
    flow_id = _flow_id(out)
    store = SessionStore()
    meta = store.load_meta(flow_id)
    assert meta["state"]["verification"]["status"] == ("passed" if passed else "failed")
    assert _state(repo) == before and not (repo / "new.txt").exists()
    assert cp.listing(engine.flow_dir(store, flow_id))[-1]["step"] == "build"

    monkeypatch.setattr("garuda.workspace.worktrees._run_check", lambda directory, command, image,
                        timeout: {"command": command, "exit_code": 0, "passed": True})
    code, out = _run(monkeypatch, capsys, "sessions", "merge", flow_id, "--check", "true",
                     "--workspace", str(repo))
    if passed:
        assert code == 0, out
    else:
        assert code == 2 and "integration.flow_unverified" in out
        assert _out(repo, "for-each-ref", "refs/garuda/integration/") == ""
