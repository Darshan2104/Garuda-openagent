"""Real native children and Git repositories own the editing-worker contract (#384)."""

import asyncio
import json
import os
import threading
from pathlib import Path

import pytest

from garuda.core.editing_worker import LocalEditingWorkers
from garuda.core.events import EventStore
from garuda.core.loop import DefaultAgent
from garuda.core.permissions import PermissionEngine
from garuda.core.subagent import DelegationBudget, SubagentRunner
from garuda.interfaces.run_guard import WorkspaceLeaseGuard
from garuda.model.protocol import ModelResponse
from garuda.model.script_model import ScriptModel
from garuda.runtime.capacity import CapacityStore
from garuda.tools import default_tools
from garuda.types import AgentConfig, Role, ToolCall
from garuda.workspace import snapshot_proto as snap
from garuda.workspace.lease import LeaseError, LeaseStore
from garuda.workspace.local import LocalEnvironment


def _call(name, **arguments):
    return ModelResponse(content=None, tool_calls=[ToolCall("call", name, arguments)])


def _done():
    return _call("task_complete", summary="Updated the requested file in the isolated worker.",
                 result={"edited": True}, verification_commands=["test -s main.txt"])


@pytest.fixture
async def editing(tmp_path, monkeypatch):
    # macOS sandbox runners may forbid ps. Supply only the current process's
    # start identity; the real lease/capacity owners still issue and persist it.
    from garuda.runtime import recovery

    monkeypatch.setattr(recovery, "_process_identity", lambda pid: "fixture-start" if pid == os.getpid() else None)
    repo = tmp_path / "parent"
    repo.mkdir()
    snap.git(repo, "init", "-q")
    (repo / "main.txt").write_text("original\n")
    snap.git(repo, "add", "main.txt")
    snap.git(repo, "commit", "-qm", "base")
    agents = tmp_path / "agents"
    agents.mkdir()
    (agents / "result.json").write_text(json.dumps({
        "type": "object", "properties": {"edited": {"type": "boolean"}}, "required": ["edited"],
    }))
    (agents / "editor.yaml").write_text(
        "version: 1\nextends: garuda/build\noutput: {schema: result.json}\n"
        "limits: {max_turns: 3}\n"
    )
    events = EventStore(persist_path=tmp_path / "session" / "events.jsonl")
    guard = WorkspaceLeaseGuard(str(repo), events.session_id)
    guard.acquire()
    workers = LocalEditingWorkers(guard, checks=["test -s main.txt"], image="checker")
    runner = SubagentRunner(
        model=ScriptModel([_call("write_file", path="main.txt", content="worker\n"), _done()]),
        env=LocalEnvironment(repo), events=events, workspace_root=str(repo),
        agents_dir=agents, parent_tools=default_tools(),
        parent_permissions=PermissionEngine(mode="smart", workspace_root=repo),
        budget=DelegationBudget(), editing_workers=workers, editing_max_tokens=1000,
    )
    try:
        yield runner, workers, repo
    finally:
        await guard.release()


def _receipt(result):
    receipt = result.metadata["editing_worker"]
    assert receipt["state"] == "ready", result.final_message
    return receipt


async def _integrate(runner, workers, receipt):
    return await workers.integrate(
        receipt["candidate_id"], receipt["provenance_digest"], env=runner.env,
        session_id=runner.events.session_id, permissions=runner.parent_permissions,
    )


async def test_native_worker_is_isolated_typed_and_explicitly_integrated(editing, monkeypatch):
    runner, workers, repo = editing
    requests = []
    complete = runner.model.complete

    async def observe(messages, **kwargs):
        requests.append((kwargs.get("max_tokens"), {t["function"]["name"] for t in kwargs["tools"]}))
        return await complete(messages, **kwargs)

    monkeypatch.setattr(runner.model, "complete", observe)
    (repo / "user.txt").write_text("preexisting untracked\n")
    (repo / "main.txt").write_text("parent dirty\n")
    snap.git(repo, "add", "main.txt")
    head = snap.git(repo, "rev-parse", "HEAD").stdout
    index = snap.git(repo, "ls-files", "-s", "-z").stdout
    result = await runner.run("editor", "edit main.txt", editing_worker=True)
    receipt = _receipt(result)
    child = Path(receipt["workspace"])
    assert child != repo and (child / ".git").is_dir()
    assert not (child / ".git" / "objects" / "info" / "alternates").exists()
    assert (child / "main.txt").read_text() == "worker\n"
    assert (repo / "main.txt").read_text() == "parent dirty\n"
    assert (child / "user.txt").read_text() == "preexisting untracked\n"
    assert result.output == {"edited": True}
    assert result.metadata["output_contract"]["validated"] is True
    assert requests and all(limit == 1000 for limit, _ in requests)
    assert all(not names.intersection({"invoke_subagent", "integrate_editing_worker", "tmux_exec"})
               for _, names in requests)
    assert receipt["candidate_id"] == result.metadata["subagent_session_id"]
    assert Path(receipt["patch"]).read_bytes().startswith(b"diff --git")
    assert not LeaseStore().holders_of(child)
    assert CapacityStore().holders("native") == []
    checked = []

    def confined_checker(directory, command, image, timeout):
        # Stand in only for Docker dispatch, observing the actual materialized
        # candidate. Snapshot construction, checks, revalidation and apply run real.
        checked.append((Path(directory, "main.txt").read_text(), command, image))
        return {"command": command, "exit_code": 0, "passed": True}

    monkeypatch.setattr("garuda.workspace.worktrees._run_check", confined_checker)
    outcome = await _integrate(runner, workers, receipt)
    assert outcome["state"] == "integrated"
    assert not child.exists()
    assert outcome["repository_state"] == "removed"
    assert checked == [("worker\n", "test -s main.txt", "checker")]
    assert (repo / "main.txt").read_text() == "worker\n"
    assert (repo / "user.txt").read_text() == "preexisting untracked\n"
    assert snap.git(repo, "rev-parse", "HEAD").stdout == head
    assert snap.git(repo, "ls-files", "-s", "-z").stdout == index
    assert json.loads((child.parent / "receipt.json").read_text())["state"] == "integrated"
    assert (await _integrate(runner, workers, receipt))["code"] == "integration.invalid_candidate"


@pytest.mark.parametrize("change", ["parent", "index", "during-check", "patch", "candidate", "check-fails", "check-mutates", "authority", "permission", "digest"])
async def test_changed_or_unverified_candidates_never_apply(editing, monkeypatch, change):
    runner, workers, repo = editing
    receipt = _receipt(await runner.run("editor", "edit", editing_worker=True))
    if change == "parent":
        (repo / "main.txt").write_text("user wins\n")
    elif change == "index":
        snap.git(repo, "update-index", "--force-remove", "main.txt")
    elif change == "patch":
        Path(receipt["patch"]).write_bytes(b"tampered")
    elif change == "candidate":
        Path(receipt["workspace"], "main.txt").write_text("tampered\n")
    elif change == "permission":
        runner.parent_permissions = PermissionEngine(mode="readonly")
    elif change == "digest":
        receipt["provenance_digest"] = "sha256:" + "0" * 64

    def check(*args):
        if change == "during-check":
            (repo / "main.txt").write_text("user wins\n")
        if change == "check-mutates":
            Path(args[0], "main.txt").write_text("unchecked\n")
        if change == "authority":
            ownership = workers.parent_guard.snapshot_ownership()["lease"]
            workers.parent_guard.leases.release(repo, runner.events.session_id, epoch=ownership["epoch"])
        return {"passed": change != "check-fails", "exit_code": 1 if change == "check-fails" else 0}

    monkeypatch.setattr("garuda.workspace.worktrees._run_check", check)
    outcome = await _integrate(runner, workers, receipt)
    assert outcome["state"] == "refused"
    assert (repo / "main.txt").read_text() == ("user wins\n" if change in {"parent", "during-check"} else "original\n")
    expected = {
        "parent": "integration.stale_base", "index": "integration.stale_base",
        "during-check": "integration.stale_base", "patch": "integration.candidate_changed",
        "candidate": "integration.candidate_changed", "check-fails": "integration.check_failed",
        "permission": "integration.permission_denied", "digest": "integration.invalid_candidate",
        "check-mutates": "integration.check_changed_tree",
        "authority": "integration.failed",
    }
    assert outcome["code"] == expected[change]


async def test_siblings_share_budget_and_stale_sibling_cannot_overwrite_winner(editing, monkeypatch):
    runner, workers, repo = editing
    first = _receipt(await runner.run("editor", "one", editing_worker=True))
    runner.model = ScriptModel([_call("write_file", path="main.txt", content="sibling\n"), _done()])
    second = _receipt(await runner.run("editor", "two", editing_worker=True))
    assert first["workspace"] != second["workspace"]
    assert first["candidate_id"] != second["candidate_id"]
    assert runner.budget.launches == 2
    monkeypatch.setattr("garuda.workspace.worktrees._run_check", lambda *a: {"passed": True})
    assert (await _integrate(runner, workers, first))["state"] == "integrated"
    assert (await _integrate(runner, workers, second))["code"] == "integration.stale_base"
    assert (repo / "main.txt").read_text() == "worker\n"
    runner.budget.max_launches = 2
    refused = await runner.run("editor", "three", editing_worker=True)
    assert refused.metadata["refused"] == "agent.delegation_exhausted"
    assert len(list(workers.root.glob("worker-*"))) == 2


async def test_cancellation_retains_worker_lease_capacity_and_uncertain_receipt(editing):
    runner, workers, repo = editing
    entered = asyncio.Event()

    class WaitingModel(ScriptModel):
        async def complete(self, *args, **kwargs):
            entered.set()
            await asyncio.Event().wait()

    runner.model = WaitingModel([])
    task = asyncio.create_task(runner.run("editor", "wait", editing_worker=True))
    await asyncio.wait_for(entered.wait(), 10)
    receipt_file, = workers.root.glob("worker-*/receipt.json")
    running = json.loads(receipt_file.read_text())
    assert CapacityStore().holders("native") == [running["candidate_id"]]
    assert LeaseStore().holders_of(running["workspace"])[0].session_id == running["candidate_id"]
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    receipt = json.loads(receipt_file.read_text())
    assert receipt["state"] == "uncertain" and receipt["cleanup"] == "unknown"
    assert Path(receipt["workspace"]).is_dir()
    assert CapacityStore().holders("native") == [receipt["candidate_id"]]
    assert LeaseStore().holders_of(receipt["workspace"])
    refused = await runner.run("editor", "retry", editing_worker=True)
    assert refused.metadata["refused"] == "worker.uncertain"
    runner.editing_workers = LocalEditingWorkers(
        workers.parent_guard, checks=list(workers.checks), image=workers.image, root=workers.root,
    )
    refused = await runner.run("editor", "retry with a fresh service", editing_worker=True)
    assert refused.metadata["refused"] == "worker.uncertain"
    assert (repo / "main.txt").read_text() == "original\n"


@pytest.mark.parametrize("check_passes", [True, False])
async def test_cancelling_integration_settles_checks_and_never_applies(editing, monkeypatch, check_passes):
    runner, workers, repo = editing
    if not check_passes:
        runner.model = ScriptModel([_done()])
    receipt = _receipt(await runner.run("editor", "edit", editing_worker=True))
    entered, finish = asyncio.Event(), threading.Event()
    loop = asyncio.get_running_loop()

    def check(*args):
        loop.call_soon_threadsafe(entered.set)
        assert finish.wait(10)
        return {"passed": check_passes}

    monkeypatch.setattr("garuda.workspace.worktrees._run_check", check)
    task = asyncio.create_task(_integrate(runner, workers, receipt))
    try:
        await asyncio.wait_for(entered.wait(), 10)
        task.cancel()
    finally:
        finish.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    retained = json.loads(Path(receipt["workspace"]).parent.joinpath("receipt.json").read_text())
    assert retained["state"] == "uncertain"
    assert (repo / "main.txt").read_text() == "original\n"
    recovered = LocalEditingWorkers(
        workers.parent_guard, checks=list(workers.checks), image=workers.image, root=workers.root,
    )
    if check_passes:
        assert (await _integrate(runner, recovered, receipt))["code"] == "worker.uncertain"
        await workers.parent_guard.release()
        discarded = workers.repositories.discard(runner.events.session_id, receipt["candidate_id"])
        assert discarded["discarded"] is True
    else:
        # A fresh owner cannot replay the candidate, but its proved no-op
        # repository can retire automatically and stop blocking admission.
        assert (await _integrate(runner, recovered, receipt))["code"] == "integration.invalid_candidate"
    assert not Path(receipt["workspace"]).exists()


async def test_ignored_parent_collision_is_not_overwritten(editing, monkeypatch):
    runner, workers, repo = editing
    runner.model = ScriptModel([_call("write_file", path="fresh.txt", content="worker\n"), _done()])
    receipt = _receipt(await runner.run("editor", "add a file", editing_worker=True))
    (repo / ".git" / "info" / "exclude").write_text("/fresh.txt\n")
    (repo / "fresh.txt").write_text("user ignored file\n")
    monkeypatch.setattr("garuda.workspace.worktrees._run_check", lambda *a: {"passed": True})
    assert (await _integrate(runner, workers, receipt))["code"] == "integration.conflicts"
    assert (repo / "fresh.txt").read_text() == "user ignored file\n"


async def test_child_model_crash_cannot_publish(editing):
    runner, workers, repo = editing

    class CrashingModel(ScriptModel):
        async def complete(self, *args, **kwargs):
            raise RuntimeError("child crash")

    runner.model = CrashingModel([])
    result = await runner.run("editor", "crash", editing_worker=True)
    assert not result.success
    # A bare model-error result has no complete cleanup report. Keep it unknown.
    assert result.metadata["editing_worker"]["state"] == "uncertain"
    assert CapacityStore().holders("native") == [result.metadata["subagent_session_id"]]
    assert (repo / "main.txt").read_text() == "original\n"
    await workers.parent_guard.release()
    from garuda.workspace.worktrees import WorktreeError

    with pytest.raises(WorktreeError, match="settlement is unproved"):
        workers.repositories.discard(runner.events.session_id, result.metadata["subagent_session_id"])
    assert CapacityStore().holders("native") == [result.metadata["subagent_session_id"]]
    assert Path(result.metadata["editing_worker"]["workspace"]).is_dir()


async def test_native_tools_deliver_typed_candidate_and_integrate_through_shared_setup(editing, monkeypatch):
    runner, workers, repo = editing
    monkeypatch.setattr("garuda.workspace.worktrees._run_check", lambda *a: {"passed": True})

    class IntegratingModel(ScriptModel):
        integrated = False

        async def complete(self, messages, *args, **kwargs):
            if self._responses:
                return await super().complete(messages, *args, **kwargs)
            if not self.integrated:
                handoff = next(m for m in messages if m.role == Role.TOOL and m.name == "invoke_subagent")
                candidate = json.loads(handoff.content.split("Editing worker candidate:\n")[1].splitlines()[0])
                self.integrated = True
                return _call("integrate_editing_worker", candidate_id=candidate["candidate_id"],
                             provenance_digest=candidate["provenance_digest"])
            return ModelResponse(content="parent finished")

    result = await DefaultAgent().run(
        "delegate an edit", IntegratingModel([
            _call("invoke_subagent", profile="editor", task="edit", editing_worker=True),
            _call("write_file", path="main.txt", content="worker\n"), _done(),
        ]), runner.env, default_tools(),
        config=AgentConfig(enable_verifier=False, max_turns=4, editing_workers=workers),
        events=runner.events, permissions=runner.parent_permissions, agents_dir=runner.agents_dir,
    )
    handoff = next(m for m in result.messages if m.role == Role.TOOL and m.name == "invoke_subagent")
    assert handoff.metadata.get("child_result"), handoff.content
    assert handoff.metadata["child_result"]["outcome"]["validation"] == "accepted"
    assert "Editing worker candidate:" in handoff.content
    integration = next(m for m in result.messages if m.role == Role.TOOL and m.name == "integrate_editing_worker")
    assert json.loads(integration.content)["state"] == "integrated"
    assert (repo / "main.txt").read_text() == "worker\n"


async def test_worker_permission_ceiling_preserves_absolute_parent_denials(editing):
    runner, _, repo = editing
    runner.parent_permissions = PermissionEngine(
        workspace_root=repo, path_rules={"deny": [str(repo / "main.txt")]},
    )
    result = await runner.run("editor", "edit denied file", editing_worker=True)
    receipt = result.metadata["editing_worker"]
    assert receipt["repository_state"] == "removed"
    assert not Path(receipt["workspace"]).exists()
    assert (repo / "main.txt").read_text() == "original\n"
    assert "Permission denied" in "\n".join(m.content for m in result.messages if m.role == Role.TOOL)


@pytest.mark.parametrize("refusal", ["capacity", "no-check", "no-schema", "foreign-parent", "unsupported", "depth"])
async def test_admission_refuses_before_child_execution(editing, refusal):
    runner, workers, repo = editing
    reservation = None
    profile = "editor"
    if refusal == "capacity":
        reservation = CapacityStore().reserve("native", "other-worker", 1)
    elif refusal == "no-check":
        workers.checks = ()
    elif refusal == "no-schema":
        profile = "garuda/build"
    elif refusal == "foreign-parent":
        runner.events = EventStore()
    elif refusal == "unsupported":
        from garuda.workspace.health import HealthMonitoredEnvironment

        class DifferentBackend(LocalEnvironment):
            pass

        runner.env = HealthMonitoredEnvironment(DifferentBackend(repo))
    elif refusal == "depth":
        runner.budget.max_depth = 0
    try:
        result = await runner.run(profile, "must not execute", editing_worker=True)
    finally:
        if reservation is not None:
            CapacityStore().release(reservation)
    expected = {
        "capacity": "worker.admission_failed", "no-check": "integration.no_trusted_check",
        "no-schema": "worker.typed_output_required", "foreign-parent": "worker.foreign_parent",
        "unsupported": "worker.unsupported_environment", "depth": "agent.delegation_too_deep",
    }
    assert not result.success and result.metadata["refused"] == expected[refusal]
    assert (repo / "main.txt").read_text() == "original\n"
    assert len(runner.model._responses) == 2


async def test_editing_worker_admits_against_the_parent_run_tree_spend_gate(editing):
    # #384 x #405: an editing worker is a native child of the run tree, so its
    # model calls admit against the root's SpendGate like any subagent's.
    from garuda.core.spend import SpendGate

    runner, workers, repo = editing
    runner.spend_gate = gate = SpendGate(max_tokens=1_000_000)
    _receipt(await runner.run("editor", "edit main.txt", editing_worker=True))
    # Both worker turns (plus any auxiliary call, e.g. contract extraction) settle here.
    assert gate.calls >= 2 and gate.settled_tokens > 0
    assert gate.reserved_tokens == 0 and gate.refused == 0


async def test_editing_worker_refused_by_the_run_tree_spend_gate_publishes_nothing(editing):
    from garuda.core.spend import SpendGate

    runner, workers, repo = editing
    runner.spend_gate = gate = SpendGate(max_tokens=1)
    result = await runner.run("editor", "edit main.txt", editing_worker=True)
    assert not result.success and gate.calls == 0 and gate.refused >= 1
    assert result.metadata.get("editing_worker", {}).get("state") != "ready"
    assert (repo / "main.txt").read_text() == "original\n"


async def test_orphan_discard_rechecks_parent_and_never_discards_another_sessions_worker(editing):
    from garuda.workspace.worktrees import WorktreeError

    runner, workers, repo = editing
    receipt = _receipt(await runner.run("editor", "edit", editing_worker=True))
    workspace = Path(receipt["workspace"])
    with pytest.raises(WorktreeError, match="identity is missing"):
        workers.repositories.discard("another-session", receipt["candidate_id"])
    directory_state = workspace.parent.stat().st_ctime_ns
    with pytest.raises(WorktreeError, match="belongs to another session"):
        workers.repositories.retire("another-session", workspace.parent.name, discard=True)
    assert workspace.parent.stat().st_ctime_ns == directory_state
    with pytest.raises(WorktreeError, match="parent workspace ownership is retained"):
        workers.repositories.discard(runner.events.session_id, receipt["candidate_id"])
    assert workspace.is_dir()
    await workers.parent_guard.release()
    workers.repositories.recover(runner.events.session_id)
    assert workspace.is_dir()  # Ready changes remain available until explicitly discarded.
    discarded = workers.repositories.discard(runner.events.session_id, receipt["candidate_id"])
    assert discarded["repository_state"] == "removed" and discarded["discarded"] is True
    assert not workspace.exists()
    assert (workspace.parent / "receipt.json").is_file()
    assert (repo / "main.txt").read_text() == "original\n"
    assert workers.repositories.discard(runner.events.session_id, receipt["candidate_id"]) == discarded


@pytest.mark.parametrize("extra", ["ignored", "head", "index", "ref", "reflog"])
async def test_automatic_cleanup_preserves_additional_worker_data(editing, monkeypatch, extra):
    runner, workers, repo = editing
    receipt = _receipt(await runner.run("editor", "edit", editing_worker=True))
    workspace = Path(receipt["workspace"])

    def check(*args):
        if extra == "ignored":
            (workspace / ".git" / "info" / "exclude").write_text("precious.txt\n")
            (workspace / "precious.txt").write_text("keep this ignored data\n")
        elif extra == "head":
            snap.git(workspace, "commit", "--allow-empty", "-qm", "unintegrated commit")
        elif extra == "reflog":
            snap.git(workspace, "commit", "--allow-empty", "-qm", "retained recovery commit")
            snap.git(workspace, "reset", "--mixed", receipt["base_commit"])
        elif extra == "index":
            snap.git(workspace, "update-index", "--force-remove", "main.txt")
        else:
            snap.git(workspace, "update-ref", "refs/heads/keep", receipt["base_commit"])
        return {"passed": True}

    monkeypatch.setattr("garuda.workspace.worktrees._run_check", check)
    outcome = await _integrate(runner, workers, receipt)
    assert outcome["state"] == "integrated"
    assert workspace.is_dir()
    await workers.parent_guard.release()
    workers.repositories.recover(runner.events.session_id)
    assert workspace.is_dir()
    assert (repo / "main.txt").read_text() == "worker\n"


@pytest.mark.parametrize("ambiguity", ["settlement", "lease", "capacity", "replacement", "legacy", "missing-lease", "missing-capacity"])
async def test_discard_never_waives_ownership_or_repository_identity(editing, ambiguity, tmp_path):
    from garuda.runtime.strict_store import write_document
    from garuda.workspace.worktrees import WorktreeError

    runner, workers, repo = editing
    receipt = _receipt(await runner.run("editor", "edit", editing_worker=True))
    workspace = Path(receipt["workspace"])
    await workers.parent_guard.release()
    receipt_path = workspace.parent / "receipt.json"
    document = json.loads(receipt_path.read_text())
    guard = None
    reservation = None
    capacity = CapacityStore()
    if ambiguity == "settlement":
        document.pop("child_settled")
    elif ambiguity == "lease":
        guard = WorkspaceLeaseGuard(str(workspace), "new-owner")
        guard.acquire()
    elif ambiguity == "capacity":
        reservation = capacity.reserve("native", receipt["candidate_id"], 1)
    elif ambiguity == "replacement":
        workspace.rename(workspace.parent / "retained-original")
        workspace.symlink_to(repo, target_is_directory=True)
    elif ambiguity == "legacy":
        document.pop("parent_ownership")
    elif ambiguity == "missing-lease":
        LeaseStore()._path_for(str(workspace)).unlink()
    else:
        CapacityStore()._path("native").unlink()
    write_document(receipt_path, document)
    try:
        with pytest.raises((WorktreeError, LeaseError)):
            workers.repositories.discard(runner.events.session_id, receipt["candidate_id"])
        assert workspace.exists()
        assert (repo / "main.txt").read_text() == "original\n"
    finally:
        if guard:
            await guard.release()
        if reservation:
            capacity.release(reservation)


async def test_worker_inventory_is_bounded_and_unknown_receipts_block_admission(editing):
    from garuda.workspace.editing_workers import MAX_WORKERS

    runner, workers, repo = editing
    receipt = _receipt(await runner.run("editor", "edit", editing_worker=True))
    for index in range(MAX_WORKERS + 1):
        (workers.root / f"worker-unknown-{index}").mkdir()
    before = {p: (p.read_bytes(), p.stat().st_mtime_ns)
              for p in workers.root.rglob("*") if p.is_file()}
    view = workers.repositories.inventory(runner.events.session_id)
    assert view["coverage"] != "complete" and view["omitted"] is None
    assert len(view["entries"]) <= MAX_WORKERS
    after = {p: (p.read_bytes(), p.stat().st_mtime_ns)
             for p in workers.root.rglob("*") if p.is_file()}
    assert before == after
    refused = await runner.run("editor", "must not relaunch", editing_worker=True)
    assert refused.metadata["refused"] == "worker.uncertain"
    assert Path(receipt["workspace"]).is_dir()


async def test_session_view_reports_uncertain_workers_without_repair(editing, monkeypatch):
    from garuda.core import read_model
    from garuda.core.sessions import SessionStore
    from garuda.runtime.strict_store import write_document

    runner, workers, repo = editing
    receipt = _receipt(await runner.run("editor", "edit", editing_worker=True))
    receipt_path = Path(receipt["workspace"]).parent / "receipt.json"
    document = json.loads(receipt_path.read_text())
    document.update(state="uncertain", cleanup="unknown")
    write_document(receipt_path, document)
    store = SessionStore()
    store.begin(runner.events.session_id, task="edit", model="fixture", agent="build", workspace=str(repo))
    before = {p: (p.read_bytes(), p.stat().st_mtime_ns)
              for p in workers.root.rglob("*") if p.is_file()}
    monkeypatch.setattr(snap, "git", lambda *a, **kw: pytest.fail("inventory ran Git"))
    view = read_model.session(store, runner.events.session_id)["ownership"]
    worker, = view["editing_workers"]["entries"]
    assert worker["candidate_id"] == receipt["candidate_id"]
    assert worker["state"] == "uncertain" and worker["cleanup"] == "unknown"
    assert "editing_worker" in view["reconciliation"]["retained"]
    assert {p: (p.read_bytes(), p.stat().st_mtime_ns)
            for p in workers.root.rglob("*") if p.is_file()} == before


async def test_cli_discard_resolves_exact_orphan_candidate(editing, capsys):
    from garuda.core.sessions import SessionStore
    from garuda.interfaces.main import build_parser, run_sessions

    runner, workers, repo = editing
    receipt = _receipt(await runner.run("editor", "edit", editing_worker=True))
    SessionStore().begin(runner.events.session_id, task="edit", model="fixture", agent="build", workspace=str(repo))
    await workers.parent_guard.release()
    args = build_parser().parse_args([
        "sessions", "discard-worker", runner.events.session_id, receipt["candidate_id"],
        "--workspace", str(repo),
    ])
    assert run_sessions(args) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["discarded"] is True and result["repository_state"] == "removed"
    assert not Path(receipt["workspace"]).exists()
    assert (repo / "main.txt").read_text() == "original\n"


@pytest.mark.parametrize("stage", ["partial", "removed"])
async def test_discard_recovers_interrupted_repository_removal(editing, monkeypatch, stage):
    import shutil
    from functools import wraps

    runner, workers, repo = editing
    receipt = _receipt(await runner.run("editor", "edit", editing_worker=True))
    workspace = Path(receipt["workspace"])
    await workers.parent_guard.release()
    remove = shutil.rmtree

    @wraps(remove)
    def interrupt(path, *args, **kwargs):
        if path == "workspace":
            if stage == "partial":
                remove(workspace / ".git")
            else:
                remove(path, *args, **kwargs)
            raise OSError("interrupted retirement")
        return remove(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(shutil, "rmtree", interrupt)
        with pytest.raises(LeaseError, match="interrupted retirement"):
            workers.repositories.discard(runner.events.session_id, receipt["candidate_id"])
    intent = json.loads((workspace.parent / "receipt.json").read_text())
    assert intent["repository_state"] == "removing"
    result = workers.repositories.discard(runner.events.session_id, receipt["candidate_id"])
    assert result["repository_state"] == "removed" and result["discarded"] is True
    assert not workspace.exists()
    assert (repo / "main.txt").read_text() == "original\n"


@pytest.mark.parametrize("flag", ["--assume-unchanged", "--skip-worktree"])
async def test_cleanup_hashes_content_hidden_from_git_diff(editing, flag):
    runner, workers, repo = editing
    runner.model = ScriptModel([_done()])
    receipt = _receipt(await runner.run("editor", "no changes", editing_worker=True))
    workspace = Path(receipt["workspace"])
    snap.git(workspace, "update-index", flag, "main.txt")
    (workspace / "main.txt").write_text("hidden unintegrated data\n")
    assert snap.git(workspace, "diff", "--quiet", receipt["base_commit"], check=False).returncode == 0
    assert (await _integrate(runner, workers, receipt))["code"] == "integration.candidate_changed"
    workers.repositories.recover(runner.events.session_id, parent_guard=workers.parent_guard)
    assert (workspace / "main.txt").read_text() == "hidden unintegrated data\n"
    assert (repo / "main.txt").read_text() == "original\n"


async def test_cleanup_command_recovers_failed_integrated_retirement(editing, monkeypatch, capsys):
    import shutil
    from functools import wraps

    from garuda.core.sessions import SessionStore
    from garuda.interfaces.main import build_parser, run_sessions

    runner, workers, repo = editing
    receipt = _receipt(await runner.run("editor", "edit", editing_worker=True))
    workspace = Path(receipt["workspace"])
    monkeypatch.setattr("garuda.workspace.worktrees._run_check", lambda *a: {"passed": True})
    remove = shutil.rmtree

    @wraps(remove)
    def fail_removal(path, *args, **kwargs):
        if path == "workspace":
            raise OSError("temporary removal failure")
        return remove(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(shutil, "rmtree", fail_removal)
        assert (await _integrate(runner, workers, receipt))["state"] == "integrated"
    assert workspace.is_dir()
    await workers.parent_guard.release()
    SessionStore().begin(runner.events.session_id, task="edit", model="fixture", agent="build", workspace=str(repo))
    args = build_parser().parse_args([
        "sessions", "cleanup-workers", runner.events.session_id, "--workspace", str(repo),
    ])
    assert run_sessions(args) == 0
    entry, = json.loads(capsys.readouterr().out)["entries"]
    assert entry["repository_state"] == "removed"
    assert not workspace.exists()
    assert (repo / "main.txt").read_text() == "worker\n"


@pytest.mark.parametrize("damage", ["json", "oversized", "symlink", "binding", "duplicate"])
async def test_unattributed_receipts_remain_unknown_and_cannot_authorize_cleanup(editing, damage):
    import shutil

    from garuda.workspace.worktrees import WorktreeError

    runner, workers, repo = editing
    receipt = _receipt(await runner.run("editor", "edit", editing_worker=True))
    workspace = Path(receipt["workspace"])
    path = workspace.parent / "receipt.json"
    if damage == "json":
        path.write_text("{")
    elif damage == "oversized":
        path.write_bytes(b" " * 65_537)
    elif damage == "symlink":
        backup = workspace.parent / "saved-receipt.json"
        path.rename(backup)
        path.symlink_to(backup)
    elif damage == "binding":
        document = json.loads(path.read_text())
        document["workspace"] = str(repo)
        path.write_text(json.dumps(document))
    else:
        copy = workers.root / "worker-duplicate"
        shutil.copytree(workspace.parent, copy)
        document = json.loads((copy / "receipt.json").read_text())
        document["workspace"] = str(copy / "workspace")
        (copy / "receipt.json").write_text(json.dumps(document))
    view = workers.repositories.inventory(runner.events.session_id)
    assert view["coverage"] == "unavailable" and view["omitted"] is None
    await workers.parent_guard.release()
    with pytest.raises(WorktreeError, match="inventory is incomplete"):
        workers.repositories.discard(runner.events.session_id, receipt["candidate_id"])
    assert workspace.is_dir()
    assert (repo / "main.txt").read_text() == "original\n"


async def test_retired_worker_path_read_error_stays_unknown(editing, monkeypatch):
    runner, workers, repo = editing
    receipt = _receipt(await runner.run("editor", "edit", editing_worker=True))
    await workers.parent_guard.release()
    workers.repositories.discard(runner.events.session_id, receipt["candidate_id"])
    workspace = Path(receipt["workspace"])
    lstat = os.lstat

    def unavailable(path, *args, **kwargs):
        if Path(path) == workspace:
            raise PermissionError("worker path cannot be inspected")
        return lstat(path, *args, **kwargs)

    monkeypatch.setattr(os, "lstat", unavailable)
    view = workers.repositories.inventory(runner.events.session_id)
    assert view["coverage"] == "unavailable" and view["omitted"] is None
