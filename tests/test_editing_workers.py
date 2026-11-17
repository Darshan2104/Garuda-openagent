"""Real native children and Git repositories own the editing-worker contract (#384)."""

import asyncio
import json
import os
import shutil
import stat
import threading
import uuid
from pathlib import Path

import pytest

from garuda.core.editing_worker import LocalEditingWorkers
from garuda.core.events import EventStore
from garuda.core.loop import DefaultAgent
from garuda.core.permissions import PermissionEngine
from garuda.core.subagent import DelegationBudget, SubagentRunner
from garuda.interfaces.run_guard import WorkspaceLeaseGuard
from garuda.model.protocol import ModelResponse, StreamDelta
from garuda.model.script_model import ScriptedStream, ScriptModel
from garuda.runtime.capacity import CapacityStore
from garuda.tools import default_tools
from garuda.types import AgentConfig, Role, ToolCall
from garuda.workspace import snapshot_proto as snap
from garuda.workspace.editing_workers import EditingWorkerRepositories
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
    # The later integration attempt pruned the first worker's retired receipt;
    # only the retained sibling remains.
    assert [path.name for path in workers.root.glob("worker-*")] == [
        Path(second["workspace"]).parent.name,
    ]


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


@pytest.mark.parametrize("ending", ["success", "cancel", "exception"])
async def test_worker_closes_its_environment_services(editing, monkeypatch, ending):
    runner, workers, repo = editing
    environments = []
    initialize = LocalEnvironment.__init__

    def capture(environment, *args, **kwargs):
        initialize(environment, *args, **kwargs)
        environments.append(environment)

    monkeypatch.setattr(LocalEnvironment, "__init__", capture)
    entered = asyncio.Event()
    process = None

    class WorkerInterrupted(BaseException):
        pass

    class Interrupted(ScriptModel):
        async def complete(self, *args, **kwargs):
            nonlocal process
            # Allocate a real optional service through the environment's public
            # API. Its owning child must close it on every exit path.
            environment, = environments
            if process is None:
                await environment.persistent_execute("true")
                process = environment._shell._proc
                assert process.returncode is None
            entered.set()
            if ending == "success":
                return await super().complete(*args, **kwargs)
            if ending == "exception":
                raise WorkerInterrupted()
            await asyncio.Event().wait()

    runner.model = Interrupted([_call("write_file", path="main.txt", content="worker\n"), _done()])
    task = asyncio.create_task(runner.run("editor", "interrupted", editing_worker=True))
    try:
        await asyncio.wait_for(entered.wait(), 10)
        if ending == "cancel":
            task.cancel()
        if ending == "success":
            _receipt(await task)
        else:
            with pytest.raises(asyncio.CancelledError if ending == "cancel" else WorkerInterrupted):
                await task
        assert process.returncode is not None
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        for environment in environments:
            await environment.aclose()
    receipt_file, = workers.root.glob("worker-*/receipt.json")
    receipt = json.loads(receipt_file.read_text())
    if ending == "success":
        assert receipt["state"] == "ready"
        assert CapacityStore().holders("native") == []
        assert not LeaseStore().holders_of(receipt["workspace"])
    else:
        assert receipt["state"] == "uncertain"
        assert CapacityStore().holders("native") == [receipt["candidate_id"]]
        assert LeaseStore().holders_of(receipt["workspace"])
    assert (repo / "main.txt").read_text() == "original\n"


@pytest.fixture
def interrupted_worker(editing, monkeypatch, request):
    runner, workers, repo = editing
    ending = getattr(request, "param", "lease")
    if ending == "return":
        return None
    entered = asyncio.Event()

    class WorkerInterrupted(BaseException):
        pass

    interrupted = {
        "exception": WorkerInterrupted(), "lease": LeaseError("lease heartbeat lost"),
        "cancel": asyncio.CancelledError("child cancelled"),
    }[ending]

    async def heartbeat():
        await entered.wait()
        raise interrupted

    def start_heartbeat(guard):
        guard._heartbeat = asyncio.create_task(heartbeat())

    class WaitingModel(ScriptModel):
        async def complete(self, *args, **kwargs):
            entered.set()
            if not isinstance(interrupted, LeaseError):
                raise interrupted
            await asyncio.Event().wait()

    if isinstance(interrupted, LeaseError):
        monkeypatch.setattr(WorkspaceLeaseGuard, "start_heartbeat", start_heartbeat)
    runner.model = WaitingModel([])
    return interrupted


@pytest.mark.parametrize("interrupted_worker", ["return", "lease", "exception", "cancel"], indirect=True)
@pytest.mark.parametrize("cancellation", ["task", "timeout"])
@pytest.mark.parametrize("close_fails", [False, True])
async def test_cancellation_during_worker_close_wins(
    editing, interrupted_worker, monkeypatch, cancellation, close_fails,
):
    runner, workers, repo = editing
    entered, finish = asyncio.Event(), asyncio.Event()
    close = LocalEnvironment.aclose
    deadline = asyncio.timeout(None)
    close_failure = OSError("sensitive close detail " * 1000)

    async def slow_close(environment):
        entered.set()
        await finish.wait()
        await close(environment)
        if close_fails:
            raise close_failure

    async def run():
        async with deadline:
            return await runner.run("editor", "interrupted close", editing_worker=True)

    monkeypatch.setattr(LocalEnvironment, "aclose", slow_close)
    task = asyncio.create_task(run())
    try:
        await asyncio.wait_for(entered.wait(), 10)
        if cancellation == "task":
            task.cancel()
        else:
            deadline.reschedule(asyncio.get_running_loop().time())
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert task.cancelling() == 1
        assert not task.done(), "cancellation returned before environment close settled"
        if cancellation == "task":
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
        finish.set()
        with pytest.raises(asyncio.CancelledError if cancellation == "task" else TimeoutError) as raised:
            await task
        if cancellation == "task":
            assert task.cancelled()
            cancelled = raised.value
        else:
            assert deadline.expired()
            cancelled = raised.value.__cause__
        if isinstance(interrupted_worker, asyncio.CancelledError):
            assert cancelled is interrupted_worker
            cancelled = cancelled.__cause__
            assert cancelled.__cause__ is (close_failure if close_fails else None)
        else:
            assert cancelled.__cause__ is (close_failure if close_fails else interrupted_worker)
    finally:
        finish.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    receipt_file, = workers.root.glob("worker-*/receipt.json")
    receipt = json.loads(receipt_file.read_text())
    assert receipt["state"] == "uncertain" and receipt["cleanup"] == "unknown"
    if close_fails:
        assert receipt["environment_close"] == "failed"
        assert "sensitive close detail" not in receipt_file.read_text()
        events = [event["payload"] for event in runner.events.get_all() if event["type"] == "editing_worker"]
        assert events[-1]["environment_close"] == "failed"
        assert "sensitive close detail" not in json.dumps(events)
    assert CapacityStore().holders("native") == [receipt["candidate_id"]]
    assert LeaseStore().holders_of(receipt["workspace"])
    assert (repo / "main.txt").read_text() == "original\n"


@pytest.mark.parametrize("interrupted_worker", ["return", "lease", "exception", "cancel"], indirect=True)
async def test_worker_records_environment_close_failure(editing, interrupted_worker, monkeypatch):
    runner, workers, repo = editing
    close_failure = OSError("sensitive close detail " * 1000)

    async def failed_close(environment):
        raise close_failure

    monkeypatch.setattr(LocalEnvironment, "aclose", failed_close)
    if isinstance(interrupted_worker, LeaseError):
        result = await runner.run("editor", "failed close", editing_worker=True)
        assert result.metadata["refused"] == "worker.admission_failed"
    else:
        expected = close_failure if interrupted_worker is None else interrupted_worker
        with pytest.raises(type(expected)) as raised:
            await runner.run("editor", "failed close", editing_worker=True)
        assert raised.value is expected
        if interrupted_worker is not None:
            assert raised.value.__cause__ is close_failure
    receipt_file, = workers.root.glob("worker-*/receipt.json")
    receipt = json.loads(receipt_file.read_text())
    assert receipt["environment_close"] == "failed"
    assert receipt["state"] == "uncertain" and receipt["cleanup"] == "unknown"
    assert "sensitive close detail" not in receipt_file.read_text()
    events = [event["payload"] for event in runner.events.get_all() if event["type"] == "editing_worker"]
    assert events[-1]["environment_close"] == "failed"
    assert "sensitive close detail" not in json.dumps(events)
    assert CapacityStore().holders("native") == [receipt["candidate_id"]]
    assert LeaseStore().holders_of(receipt["workspace"])
    assert (repo / "main.txt").read_text() == "original\n"


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
        "capacity": "worker.capacity_unavailable", "no-check": "integration.no_trusted_check",
        "no-schema": "worker.typed_output_required", "foreign-parent": "worker.foreign_parent",
        "unsupported": "worker.unsupported_environment", "depth": "agent.delegation_too_deep",
    }
    assert not result.success and result.metadata["refused"] == expected[refusal]
    assert (repo / "main.txt").read_text() == "original\n"
    assert len(runner.model._responses) == 2


async def test_capacity_refused_admission_settles_and_later_admission_proceeds(editing, monkeypatch):
    runner, workers, repo = editing
    capacity = CapacityStore()
    reservation = capacity.reserve("native", "other-worker", 1)
    head = snap.git(repo, "rev-parse", "HEAD").stdout
    index = snap.git(repo, "ls-files", "-s", "-z").stdout
    try:
        refused = await runner.run("editor", "must not execute", editing_worker=True)
        assert not refused.success
        assert refused.metadata["refused"] == "worker.capacity_unavailable"
        receipt = refused.metadata["editing_worker"]
        assert receipt["state"] == "refused" and receipt["cleanup"] == "settled"
        assert receipt["child_settled"] is True and receipt["ownership"] == "never_acquired"
        assert receipt["admission_refused"] == "worker.capacity_unavailable"
        assert receipt["repository_state"] == "removed" and receipt["retired_ns"] > 0
        directory = Path(receipt["workspace"]).parent
        assert directory.is_dir() and not os.path.lexists(receipt["workspace"])
        inventory = EditingWorkerRepositories().inventory(runner.events.session_id)
        assert inventory["coverage"] == "complete"
        entry, = inventory["entries"]
        assert entry["candidate_id"] == receipt["candidate_id"]
        assert entry["repository_state"] == "removed"
        assert not LeaseStore().holders_of(receipt["workspace"])
        assert not capacity.slots_for_holder(receipt["candidate_id"])
        assert capacity.holders("native") == ["other-worker"]
        assert (repo / "main.txt").read_text() == "original\n"
        assert snap.git(repo, "rev-parse", "HEAD").stdout == head
        assert snap.git(repo, "ls-files", "-s", "-z").stdout == index
        assert snap.git(repo, "status", "--porcelain").stdout == b""
        assert len(runner.model._responses) == 2
    finally:
        capacity.release(reservation)

    ready = _receipt(await runner.run("editor", "edit main.txt", editing_worker=True))
    checked = []

    def confined_checker(directory, command, image, timeout):
        checked.append((Path(directory, "main.txt").read_text(), command, image))
        return {"command": command, "exit_code": 0, "passed": True}

    monkeypatch.setattr("garuda.workspace.worktrees._run_check", confined_checker)
    assert (await _integrate(runner, workers, ready))["state"] == "integrated"
    assert checked == [("worker\n", "test -s main.txt", "checker")]
    assert (repo / "main.txt").read_text() == "worker\n"
    workers.repositories.recover(runner.events.session_id)
    assert not directory.exists()
    events = [json.loads(line) for line in runner.events.persist_path.read_text().splitlines()]
    assert any(event["type"] == "editing_worker" and event["payload"] == receipt for event in events)


async def test_capacity_refusal_receipt_write_failure_retains_quarantine(editing, monkeypatch):
    from garuda.runtime.strict_store import StorageUnavailable

    runner, workers, repo = editing
    capacity = CapacityStore()
    reservation = capacity.reserve("native", "other-worker", 1)
    replace = os.replace
    failed = False

    def blocked_receipt(source, destination, **kwargs):
        nonlocal failed
        if destination == "receipt.json" and not failed:
            directory_fd = kwargs["dst_dir_fd"]
            fd = os.open(source, os.O_RDONLY, dir_fd=directory_fd)
            with os.fdopen(fd) as handle:
                document = json.load(handle)
            if document["state"] == "refused":
                failed = True
                # A directory at the receipt path makes the real atomic
                # replacement fail. Restore writability for quarantine's write.
                os.unlink(destination, dir_fd=directory_fd)
                os.mkdir(destination, mode=0o700, dir_fd=directory_fd)
                try:
                    return replace(source, destination, **kwargs)
                finally:
                    os.rmdir(destination, dir_fd=directory_fd)
                    os.unlink(source, dir_fd=directory_fd)
        return replace(source, destination, **kwargs)

    try:
        with monkeypatch.context() as patch:
            patch.setattr(os, "replace", blocked_receipt)
            with pytest.raises(StorageUnavailable, match="cannot publish.*receipt.json"):
                await runner.run("editor", "must not execute", editing_worker=True)
    finally:
        capacity.release(reservation)

    assert failed
    receipt_file, = workers.root.glob("worker-*/receipt.json")
    receipt = json.loads(receipt_file.read_text())
    assert receipt["state"] == "uncertain" and receipt["cleanup"] == "unknown"
    assert receipt.get("repository_state", "retained") == "retained"
    assert "retired_ns" not in receipt
    workers.repositories.recover(runner.events.session_id, parent_guard=workers.parent_guard)
    assert json.loads(receipt_file.read_text()) == receipt
    retry = await runner.run("editor", "still must not execute", editing_worker=True)
    assert not retry.success and retry.metadata["refused"] == "worker.uncertain"
    assert (repo / "main.txt").read_text() == "original\n"
    assert len(runner.model._responses) == 2


async def test_capacity_refusal_with_existing_workspace_quarantines_admission(editing, monkeypatch):
    runner, workers, repo = editing
    capacity = CapacityStore()
    reservation = capacity.reserve("native", "other-worker", 1)
    rename = os.rename
    created = []

    def existing_workspace(source, destination, *args, **kwargs):
        result = rename(source, destination, *args, **kwargs)
        destination = Path(destination)
        if destination.parent == workers.root and destination.name.startswith("worker-"):
            workspace = destination / "workspace"
            workspace.mkdir()
            (workspace / "retained.txt").write_text("pre-existing data\n")
            created.append(workspace)
        return result

    try:
        with monkeypatch.context() as patch:
            patch.setattr(os, "rename", existing_workspace)
            refused = await runner.run("editor", "must not execute", editing_worker=True)
    finally:
        capacity.release(reservation)

    assert not refused.success and refused.metadata["refused"] == "worker.admission_failed"
    workspace, = created
    receipt_file = workspace.parent / "receipt.json"
    receipt = json.loads(receipt_file.read_text())
    assert receipt["state"] == "uncertain" and receipt["cleanup"] == "unknown"
    assert receipt.get("repository_state", "retained") == "retained"
    workers.repositories.recover(runner.events.session_id, parent_guard=workers.parent_guard)
    assert json.loads(receipt_file.read_text()) == receipt
    retry = await runner.run("editor", "still must not execute", editing_worker=True)
    assert not retry.success and retry.metadata["refused"] == "worker.uncertain"
    assert (workspace / "retained.txt").read_text() == "pre-existing data\n"
    assert (repo / "main.txt").read_text() == "original\n"
    assert len(runner.model._responses) == 2


async def test_malformed_capacity_store_quarantines_admission_and_blocks_retry(editing):
    runner, workers, repo = editing
    capacity = CapacityStore()
    reservation = capacity.reserve("native", "other-worker", 1)
    capacity.release(reservation)
    capacity._path("native").write_text('{"version":')

    refused = await runner.run("editor", "must not execute", editing_worker=True)
    assert not refused.success and refused.metadata["refused"] == "worker.admission_failed"
    receipt_file, = workers.root.glob("worker-*/receipt.json")
    receipt = json.loads(receipt_file.read_text())
    assert receipt["state"] == "uncertain" and receipt["cleanup"] == "unknown"
    retry = await runner.run("editor", "still must not execute", editing_worker=True)
    assert not retry.success and retry.metadata["refused"] == "worker.uncertain"
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


async def test_worker_inventory_reports_unknown_receipts_and_blocks_admission(editing):
    from garuda.workspace.editing_workers import MAX_WORKERS

    runner, workers, repo = editing
    receipt = _receipt(await runner.run("editor", "edit", editing_worker=True))
    for index in range(MAX_WORKERS + 1):
        unknown = workers.root / f"worker-unknown-{index}"
        unknown.mkdir()
        (unknown / "unattributed").write_text("not a receipt\n")
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


async def test_failed_source_validation_leaves_no_worker_entry_and_other_sessions_admit(editing):
    runner, workers, repo = editing
    (repo / ".git").rename(repo / "git-backup")
    try:
        failed = await runner.run("editor", "must not create a worker", editing_worker=True)
        assert list(workers.root.glob("worker-*")) == []
        assert not failed.success and failed.metadata["refused"] == "snapshot.not_git"
        assert workers.repositories.inventory("another-session")["coverage"] == "complete"
    finally:
        (repo / "git-backup").rename(repo / ".git")
    receipt = _receipt(await runner.run("editor", "admit after a refusal", editing_worker=True))
    assert Path(receipt["workspace"]).parent.is_dir()


async def test_failed_admission_receipt_never_publishes_a_worker(editing, monkeypatch):
    from garuda.runtime.strict_store import StorageUnavailable

    runner, workers, repo = editing

    def failed_write(*args, **kwargs):
        raise StorageUnavailable("receipt write failed")

    with monkeypatch.context() as patch:
        patch.setattr("garuda.core.editing_worker.write_locked_document", failed_write)
        with pytest.raises(StorageUnavailable, match="receipt write failed"):
            await runner.run("editor", "must not publish a worker", editing_worker=True)
    assert list(workers.root.glob("worker-*")) == []
    assert workers.repositories.inventory("another-session")["coverage"] == "complete"
    _receipt(await runner.run("editor", "admit after a write failure", editing_worker=True))


@pytest.mark.parametrize("name", ["worker-vanished", ".admit-vanished"])
async def test_worker_inventory_skips_entries_removed_after_scan(editing, monkeypatch, name):
    from contextlib import contextmanager

    runner, workers, repo = editing
    receipt = _receipt(await runner.run("editor", "edit", editing_worker=True))
    vanished = workers.root / name
    vanished.mkdir(mode=0o700)
    scandir = os.scandir

    @contextmanager
    def removing_scan(path):
        with scandir(path) as entries:
            listing = list(entries)
            for entry in listing:
                if entry.name == name:
                    assert entry.is_dir(follow_symlinks=False)
                    vanished.rmdir()
            yield iter(listing)

    monkeypatch.setattr(os, "scandir", removing_scan)
    view = workers.repositories.inventory(runner.events.session_id)
    assert view["coverage"] == "complete" and view["omitted"] == 0
    assert [entry["candidate_id"] for entry in view["entries"]] == [receipt["candidate_id"]]
    assert view["orphans"] == []
    assert not vanished.exists()


async def test_receiptless_empty_and_staging_workers_are_reported_and_cleanable(editing):
    runner, workers, repo = editing
    workers.root.mkdir(mode=0o700, parents=True)
    empty = workers.root / "worker-empty"
    staging = workers.root / ".admit-abandoned"
    empty.mkdir()
    staging.mkdir()
    candidate = str(uuid.uuid4())
    admitted = workers.root / f".admit-{candidate}-unpublished"
    admitted.mkdir(mode=0o700)
    from garuda.runtime.strict_store import write_document

    write_document(admitted / "receipt.json", {
        "version": 1, "candidate_id": candidate, "state": "admitted",
        "workspace": str(workers.root / f"worker-{candidate}" / "workspace"),
    })
    interrupted = workers.root / f".admit-{uuid.uuid4()}-interrupted"
    interrupted.mkdir(mode=0o700)
    for name in (".lock", f".receipt.json.{os.getpid()}.{uuid.uuid4().hex}.tmp"):
        path = interrupted / name
        path.write_text('{"version":')
        os.chmod(path, 0o600)
    view = workers.repositories.inventory(runner.events.session_id)
    assert view["coverage"] == "complete"
    assert {orphan["directory"] for orphan in view["orphans"]} == {
        empty.name, staging.name, admitted.name, interrupted.name,
    }
    assert {orphan["state"] for orphan in view["orphans"]} == {"empty", "staging"}

    _receipt(await runner.run("editor", "admit beside removable leftovers", editing_worker=True))
    assert not any(path.exists() for path in (empty, staging, admitted, interrupted))
    assert workers.repositories.inventory(runner.events.session_id)["coverage"] == "complete"


@pytest.mark.parametrize("name", ["worker-empty", ".admit-abandoned"])
async def test_cleanup_preserves_unattributed_nonempty_directories(editing, name):
    runner, workers, repo = editing
    orphan = workers.root / name
    orphan.mkdir(mode=0o700, parents=True)
    data = orphan / "precious.txt"
    data.write_text("keep unowned data\n")
    view = workers.repositories.recover(runner.events.session_id)
    assert view["coverage"] == "unavailable" and view["omitted"] is None
    assert data.read_text() == "keep unowned data\n"


async def test_retired_receipts_are_pruned_by_a_later_cleanup_run(editing, monkeypatch):
    runner, workers, repo = editing
    receipt = _receipt(await runner.run("editor", "edit", editing_worker=True))
    directory = Path(receipt["workspace"]).parent
    monkeypatch.setattr("garuda.workspace.worktrees._run_check", lambda *a: {"passed": True})
    integrated = await _integrate(runner, workers, receipt)
    assert integrated["repository_state"] == "removed" and directory.is_dir()

    from garuda.runtime.strict_store import write_document
    from garuda.workspace.editing_workers import MAX_WORKERS

    document = json.loads((directory / "receipt.json").read_text())
    uncertain = workers.root / "worker-uncertain"
    uncertain_receipt = {
        **document, "candidate_id": str(uuid.uuid4()), "state": "uncertain", "cleanup": "unknown",
        "workspace": str(uncertain / "workspace"),
    }
    write_document(uncertain / "receipt.json", uncertain_receipt)
    for index in range(MAX_WORKERS + 1):
        retired = workers.root / f"worker-retired-{index}"
        write_document(retired / "receipt.json", {
            **document, "candidate_id": str(uuid.uuid4()),
            "workspace": str(retired / "workspace"),
        })
    workers.repositories.recover(runner.events.session_id)
    assert list(workers.root.glob("worker-*")) == [uncertain]
    assert json.loads((uncertain / "receipt.json").read_text()) == uncertain_receipt
    assert workers.repositories.inventory(runner.events.session_id)["coverage"] == "complete"
    assert (repo / "main.txt").read_text() == "worker\n"


async def test_unrelated_session_receipts_do_not_bound_this_session(editing):
    from garuda.workspace.editing_workers import MAX_WORKERS

    runner, workers, repo = editing
    receipt = _receipt(await runner.run("editor", "edit", editing_worker=True))
    document = json.loads((Path(receipt["workspace"]).parent / "receipt.json").read_text())
    for index in range(MAX_WORKERS * 32 + 1):
        directory = workers.root / f"worker-other-{index}"
        directory.mkdir(mode=0o700)
        other = {
            **document, "candidate_id": str(uuid.uuid4()), "parent_session_id": "other-session",
            "workspace": str(directory / "workspace"),
        }
        record = directory / "receipt.json"
        record.write_text(json.dumps(other))
        os.chmod(record, 0o600)

    view = workers.repositories.inventory(runner.events.session_id)
    assert view["coverage"] == "complete"
    assert [entry["candidate_id"] for entry in view["entries"]] == [receipt["candidate_id"]]

    from garuda.runtime.strict_store import write_document

    for index in range(MAX_WORKERS):
        directory = workers.root / f"worker-other-{index}"
        other = json.loads((directory / "receipt.json").read_text())
        other["parent_session_id"] = runner.events.session_id
        write_document(directory / "receipt.json", other)
    bounded = workers.repositories.inventory(runner.events.session_id)
    assert bounded["coverage"] == "bounded" and bounded["omitted"] is None
    assert len(bounded["entries"]) == MAX_WORKERS
    refused = await runner.run("editor", "must not exceed this session's bound", editing_worker=True)
    assert refused.metadata["refused"] == "worker.uncertain"


async def test_worker_store_is_private_under_umask_002(editing, tmp_path, monkeypatch):
    runner, workers, repo = editing
    monkeypatch.setenv("GARUDA_GLOBAL_SETTINGS", str(tmp_path / "fresh-home" / ".agent" / "settings.yaml"))
    workers.root = tmp_path / "fresh-home" / ".agent" / "worktrees" / "editing-workers"
    workers.repositories = EditingWorkerRepositories(workers.root)
    previous = os.umask(0o002)
    try:
        receipt = _receipt(await runner.run("editor", "edit", editing_worker=True))
    finally:
        os.umask(previous)
    assert stat.S_IMODE(workers.root.stat().st_mode) == 0o700
    assert stat.S_IMODE(workers.root.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(Path(receipt["workspace"]).parent.stat().st_mode) == 0o700
    # Only the worker root and its entries are the privacy boundary; a
    # group-readable ancestor such as the user's home must not refuse the store.
    os.chmod(workers.root.parent, 0o775)
    assert workers.repositories.inventory(runner.events.session_id)["coverage"] == "complete"


def test_untrusted_ancestor_error_names_the_refused_path(tmp_path):
    from garuda.runtime.private_records import open_directory
    from garuda.runtime.strict_store import StorageUnavailable

    shared = tmp_path / "shared"
    shared.mkdir()
    os.chmod(shared, 0o777)
    inner = shared / "inner"
    inner.mkdir(mode=0o700)
    with pytest.raises(StorageUnavailable, match=str(shared)):
        open_directory(inner)


async def test_retirement_proof_and_removal_run_outside_global_locks(editing, monkeypatch):
    from contextlib import contextmanager

    runner, workers, repo = editing
    receipt = _receipt(await runner.run("editor", "edit", editing_worker=True))
    held = set()
    lease_guard, capacity_guard = LeaseStore.guard_released, CapacityStore.guard_released
    git, capture, remove = snap.git, snap.capture, shutil.rmtree
    observed = set()

    @contextmanager
    def tracked_lease(self, workspaces):
        held.add("lease")
        try:
            with lease_guard(self, workspaces):
                yield
        finally:
            held.discard("lease")

    @contextmanager
    def tracked_capacity(self, key, holder):
        held.add("capacity")
        try:
            with capacity_guard(self, key, holder):
                yield
        finally:
            held.discard("capacity")

    def tracked_git(*args, **kwargs):
        assert not held, "Git proof ran while global locks were held"
        observed.add("git")
        return git(*args, **kwargs)

    def tracked_capture(*args, **kwargs):
        assert not held, "content proof ran while global locks were held"
        observed.add("hash")
        return capture(*args, **kwargs)

    def tracked_remove(*args, **kwargs):
        assert not held, "repository removal ran while global locks were held"
        observed.add("removal")
        return remove(*args, **kwargs)

    tracked_remove.avoids_symlink_attacks = remove.avoids_symlink_attacks
    monkeypatch.setattr(LeaseStore, "guard_released", tracked_lease)
    monkeypatch.setattr(CapacityStore, "guard_released", tracked_capacity)
    monkeypatch.setattr(snap, "git", tracked_git)
    monkeypatch.setattr(snap, "capture", tracked_capture)
    monkeypatch.setattr(shutil, "rmtree", tracked_remove)
    monkeypatch.setattr("garuda.workspace.worktrees._run_check", lambda *a: {"passed": True})
    result = await _integrate(runner, workers, receipt)
    assert result["repository_state"] == "removed"
    assert observed == {"git", "hash", "removal"}


@pytest.mark.parametrize("changed", ["receipt", "parent-lease"])
async def test_retirement_rechecks_receipt_after_content_proof(editing, monkeypatch, changed):
    from garuda.runtime.strict_store import write_document

    runner, workers, repo = editing
    receipt = _receipt(await runner.run("editor", "edit", editing_worker=True))
    directory = Path(receipt["workspace"]).parent
    capture = snap.capture

    def changed_receipt(source, **kwargs):
        result = capture(source, **kwargs)
        if kwargs.get("object_repo") is not None:
            if changed == "receipt":
                document = json.loads((directory / "receipt.json").read_text())
                document["child_settled"] = False
                write_document(directory / "receipt.json", document)
            else:
                write_document(workers.parent_guard.leases._path_for(str(repo)), {"version": 2, "holders": []})
        return result

    monkeypatch.setattr(snap, "capture", changed_receipt)
    monkeypatch.setattr("garuda.workspace.worktrees._run_check", lambda *a: {"passed": True})
    assert (await _integrate(runner, workers, receipt))["state"] == "integrated"
    assert Path(receipt["workspace"]).is_dir()
    durable = json.loads((directory / "receipt.json").read_text())
    if changed == "receipt":
        assert durable["child_settled"] is False
    assert durable.get("repository_state", "retained") == "retained"


async def test_admission_and_retirement_run_store_work_off_the_event_loop(editing, monkeypatch):
    runner, workers, repo = editing
    main_thread = threading.get_ident()
    recovered, retired = [], []
    recover, retire = EditingWorkerRepositories.recover, EditingWorkerRepositories.retire

    def observed_recover(self, *args, **kwargs):
        recovered.append(threading.get_ident())
        return recover(self, *args, **kwargs)

    def observed_retire(self, *args, **kwargs):
        retired.append(threading.get_ident())
        return retire(self, *args, **kwargs)

    monkeypatch.setattr(EditingWorkerRepositories, "recover", observed_recover)
    receipt = _receipt(await runner.run("editor", "edit", editing_worker=True))
    assert recovered and all(thread != main_thread for thread in recovered)
    monkeypatch.setattr("garuda.workspace.worktrees._run_check", lambda *a: {"passed": True})
    monkeypatch.setattr(EditingWorkerRepositories, "retire", observed_retire)
    assert (await _integrate(runner, workers, receipt))["state"] == "integrated"
    assert retired and all(thread != main_thread for thread in retired)


@pytest.mark.parametrize("operation", ["recover", "retire", "failed-removal"])
async def test_cancellation_settles_repository_threads_before_returning(editing, monkeypatch, operation):
    runner, workers, repo = editing
    receipt = _receipt(await runner.run("editor", "edit", editing_worker=True))
    entered, finish = asyncio.Event(), threading.Event()
    loop = asyncio.get_running_loop()
    method = getattr(EditingWorkerRepositories, "retire" if operation == "failed-removal" else operation)

    def blocked(self, *args, **kwargs):
        if operation == "retire" and (repo / "main.txt").read_text() != "worker\n":
            return method(self, *args, **kwargs)
        loop.call_soon_threadsafe(entered.set)
        assert finish.wait(10)
        return method(self, *args, **kwargs)

    if operation == "failed-removal":
        remove = shutil.rmtree

        def interrupted(path, *args, **kwargs):
            if path != "workspace.retiring":
                return remove(path, *args, **kwargs)
            loop.call_soon_threadsafe(entered.set)
            assert finish.wait(10)
            raise OSError("interrupted removal")

        interrupted.avoids_symlink_attacks = remove.avoids_symlink_attacks
        monkeypatch.setattr(shutil, "rmtree", interrupted)
    else:
        monkeypatch.setattr(EditingWorkerRepositories, operation, blocked)
    monkeypatch.setattr("garuda.workspace.worktrees._run_check", lambda *a: {"passed": True})
    task = asyncio.create_task(_integrate(runner, workers, receipt))
    try:
        await asyncio.wait_for(entered.wait(), 10)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done(), "cancellation returned while the repository thread was running"
    finally:
        finish.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    directory = Path(receipt["workspace"]).parent
    durable = json.loads((directory / "receipt.json").read_text())
    if operation == "failed-removal":
        assert durable["repository_state"] == "removing"
        assert (directory / "workspace.retiring").is_dir()
    elif operation == "retire":
        assert durable["repository_state"] == "removed"
        assert not Path(receipt["workspace"]).exists()
        assert (repo / "main.txt").read_text() == "worker\n"
    else:
        assert durable["state"] == "ready"
        assert (repo / "main.txt").read_text() == "original\n"


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
    retiring = workspace.parent / "workspace.retiring"
    await workers.parent_guard.release()
    remove = shutil.rmtree

    @wraps(remove)
    def interrupt(path, *args, **kwargs):
        if path == "workspace.retiring":
            if stage == "partial":
                remove(retiring / ".git")
            else:
                remove(path, *args, **kwargs)
            raise OSError("interrupted retirement")
        return remove(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(shutil, "rmtree", interrupt)
        with pytest.raises(OSError, match="interrupted retirement"):
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
        if path == "workspace.retiring":
            raise OSError("temporary removal failure")
        return remove(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(shutil, "rmtree", fail_removal)
        assert (await _integrate(runner, workers, receipt))["state"] == "integrated"
    assert (workspace.parent / "workspace.retiring").is_dir()
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



class WorkerCrash(BaseException):
    """A fatal child failure that escapes the loop's ordinary model retries."""


class TaskScriptModel(ScriptModel):
    """Choose a script from the actual child task, with bounded rendezvous."""

    def __init__(self, *, barrier=None, waits=None, scripts=None):
        super().__init__([])
        self.scripts = scripts or {
            name: [_call("write_file", path=f"{name[0]}.txt", content=f"{name}\n"), _done()]
            for name in ("alpha", "beta")
        }
        self.barrier = barrier
        self.waits = waits or {}
        self.calls = []
        self.reached = {(name, index): asyncio.Event()
                        for name, script in self.scripts.items() for index in range(len(script))}

    async def complete(self, messages, **kwargs):
        name = next(name for name in self.scripts
                    if any(m.role == Role.USER and name in m.content for m in messages))
        index = self.calls.count(name)
        self.calls.append(name)
        if index >= len(self.scripts[name]):
            return ModelResponse(content="Done.", tool_calls=[])
        if index == 0 and self.barrier is not None:
            await asyncio.wait_for(self.barrier.wait(), 10)
        self.reached[name, index].set()
        if (name, index) in self.waits:
            await asyncio.wait_for(self.waits[name, index].wait(), 10)
        response = self.scripts[name][index]
        if isinstance(response, BaseException):
            raise response
        return response

    async def stream(self, messages, **kwargs):
        response = await self.complete(messages, **kwargs)
        async for delta in ScriptModel([response]).stream(messages, **kwargs):
            yield delta
            await asyncio.sleep(0)


def _concurrent_workers(editing, model):
    runner, previous, repo = editing
    workers = LocalEditingWorkers(
        previous.parent_guard, checks=list(previous.checks), image=previous.image,
        root=previous.root, max_concurrent=2,
    )
    runner.editing_workers, runner.model = workers, model
    return runner, workers, repo


@pytest.mark.parametrize("operation", ["run", "integrate"])
async def test_default_worker_limit_refuses_live_overlap_as_busy(editing, operation):
    runner, workers, repo = editing
    release = asyncio.Event()
    model = runner.model = TaskScriptModel(waits={("beta", 0): release})
    ready = _receipt(await workers.run(runner, "editor", "alpha"))
    live = asyncio.create_task(workers.run(runner, "editor", "beta"))
    try:
        await asyncio.wait_for(model.reached["beta", 0].wait(), 10)
        receipts = [json.loads(path.read_text()) for path in workers.root.glob("worker-*/receipt.json")]
        assert sorted(r["state"] for r in receipts) == ["ready", "running"]
        directories = set(workers.root.glob("worker-*"))
        calls = list(model.calls)
        if operation == "run":
            refused = await workers.run(runner, "editor", "another alpha")
            assert refused.metadata["refused"] == "worker.busy"
        else:
            assert (await _integrate(runner, workers, ready))["code"] == "worker.busy"
        assert set(workers.root.glob("worker-*")) == directories
        assert model.calls == calls
        assert (repo / "main.txt").read_text() == "original\n"
        assert not (repo / "a.txt").exists() and not (repo / "b.txt").exists()
    finally:
        release.set()
        _receipt(await live)


async def test_admission_rechecks_live_siblings_after_recovery(editing, monkeypatch):
    import fcntl

    from garuda.runtime.strict_store import StorageUnavailable, exclusive_lock

    model = TaskScriptModel(waits={("beta", 0): asyncio.Event()})
    runner, workers, repo = _concurrent_workers(editing, model)
    beta = asyncio.create_task(workers.run(runner, "editor", "beta"))
    alpha = None
    try:
        await asyncio.wait_for(model.reached["beta", 0].wait(), 10)
        receipt_path, = workers.root.glob("worker-*/receipt.json")
        running = json.loads(receipt_path.read_text())
        assert running["state"] == "running"
        recovering = asyncio.Event()
        loop = asyncio.get_running_loop()
        flock = fcntl.flock
        # Recovery needs this real root lock before inspecting receipts. Observe
        # its lock attempt without replacing recovery or manufacturing inventory.
        with exclusive_lock(workers.root):
            root_lock = (workers.root / ".lock").stat()

            def observe_lock(fd, operation):
                info = os.fstat(fd)
                if (info.st_dev, info.st_ino) == (root_lock.st_dev, root_lock.st_ino):
                    loop.call_soon_threadsafe(recovering.set)
                return flock(fd, operation)

            with monkeypatch.context() as patch:
                patch.setattr(fcntl, "flock", observe_lock)
                alpha = asyncio.create_task(workers.run(runner, "editor", "alpha"))
                await asyncio.wait_for(recovering.wait(), 10)
                assert not alpha.done() and not beta.done()
                # A directory at the receipt lock path makes quarantine's real
                # write fail while leaving the published running receipt intact.
                receipt_lock = receipt_path.parent / ".lock"
                receipt_lock.unlink()
                receipt_lock.mkdir(mode=0o700)
                try:
                    beta.cancel()
                    with pytest.raises(asyncio.CancelledError) as cancelled:
                        await beta
                    assert isinstance(cancelled.value.__cause__, StorageUnavailable)
                    assert json.loads(receipt_path.read_text()) == running
                finally:
                    receipt_lock.rmdir()
                # Restore an ordinary lock so only the exited running receipt
                # can explain admission refusal, not unreadable storage.
                with exclusive_lock(receipt_path.parent):
                    pass
        refused = await asyncio.wait_for(alpha, 10)
        assert not refused.success
        assert refused.metadata["refused"] == "worker.uncertain"
        assert model.calls == ["beta"]
        assert json.loads(receipt_path.read_text()) == running
        assert workers.repositories.inventory(runner.events.session_id)["coverage"] == "complete"
        assert CapacityStore().holders("native") == [running["candidate_id"]]
        assert [h.session_id for h in LeaseStore().holders_of(running["workspace"])] == [running["candidate_id"]]
        assert (repo / "main.txt").read_text() == "original\n"
    finally:
        for task in (alpha, beta):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(*(task for task in (alpha, beta) if task is not None), return_exceptions=True)


async def test_concurrent_workers_run_together_and_integrate_serially(editing, monkeypatch):
    from garuda.core.spend import SpendGate

    start, finish_beta, alpha_ready = asyncio.Event(), asyncio.Event(), asyncio.Event()
    model = TaskScriptModel(barrier=asyncio.Barrier(2), waits={
        ("alpha", 0): start, ("beta", 0): start, ("beta", 1): finish_beta,
    })
    runner, workers, repo = _concurrent_workers(editing, model)
    runner.spend_gate = SpendGate(max_tokens=1_000_000)
    ready = []

    def observe(event, store):
        receipt = event["payload"]
        if (event["type"] == "editing_worker" and receipt.get("state") == "ready"
                and receipt.get("child_settled")):
            ready.append(receipt)
            alpha_ready.set()

    runner.events.add_observer(observe)
    batch = asyncio.create_task(runner.run_editing_workers([("editor", "alpha"), ("editor", "beta")]))
    try:
        for name in ("alpha", "beta"):
            await asyncio.wait_for(model.reached[name, 0].wait(), 10)
        receipts = [json.loads(path.read_text()) for path in workers.root.glob("worker-*/receipt.json")]
        assert len(receipts) == 2 and all(r["state"] == "running" for r in receipts)
        ids = {r["candidate_id"] for r in receipts}
        assert set(CapacityStore().holders("native")) == ids
        for receipt in receipts:
            holders = LeaseStore().holders_of(receipt["workspace"])
            assert [holder.session_id for holder in holders] == [receipt["candidate_id"]]
        directories = set(workers.root.glob("worker-*"))
        third = await workers.run(runner, "editor", "third")
        assert third.metadata["refused"] == "worker.busy"
        assert set(workers.root.glob("worker-*")) == directories
        start.set()
        await asyncio.wait_for(alpha_ready.wait(), 10)
        await asyncio.wait_for(model.reached["beta", 1].wait(), 10)
        assert (await _integrate(runner, workers, ready[0]))["code"] == "worker.busy"
        assert not (repo / "a.txt").exists() and not (repo / "b.txt").exists()
    finally:
        start.set()
        finish_beta.set()
        results = await batch
    alpha, beta = map(_receipt, results)
    assert (Path(alpha["workspace"]) / "a.txt").read_text() == "alpha\n"
    assert (Path(beta["workspace"]) / "b.txt").read_text() == "beta\n"
    # Only Docker dispatch is replaced; candidate construction and application
    # still cross the real repository, permission and ownership boundaries.
    checking, finish_check = asyncio.Event(), threading.Event()
    loop = asyncio.get_running_loop()

    def check(*args):
        loop.call_soon_threadsafe(checking.set)
        assert finish_check.wait(10)
        return {"passed": True}

    monkeypatch.setattr("garuda.workspace.worktrees._run_check", check)
    integrating = asyncio.create_task(_integrate(runner, workers, alpha))
    try:
        await asyncio.wait_for(checking.wait(), 10)
        assert (await _integrate(runner, workers, beta))["code"] == "worker.busy"
        directories = set(workers.root.glob("worker-*"))
        refused = await workers.run(runner, "editor", "third")
        assert refused.metadata["refused"] == "worker.busy"
        assert set(workers.root.glob("worker-*")) == directories
    finally:
        finish_check.set()
        integrated = await integrating
    assert integrated["state"] == "integrated"
    assert (await _integrate(runner, workers, beta))["code"] == "integration.stale_base"
    assert (repo / "a.txt").read_text() == "alpha\n" and not (repo / "b.txt").exists()
    assert runner.budget.launches == 2
    assert runner.spend_gate.reserved_tokens == 0


async def test_cancelling_a_batch_quarantines_every_live_sibling(editing):
    blocked = asyncio.Event()
    model = TaskScriptModel(barrier=asyncio.Barrier(2), waits={
        ("alpha", 0): blocked, ("beta", 0): blocked,
    })
    runner, workers, repo = _concurrent_workers(editing, model)
    batch = asyncio.create_task(runner.run_editing_workers([("editor", "alpha"), ("editor", "beta")]))
    try:
        for name in ("alpha", "beta"):
            await asyncio.wait_for(model.reached[name, 0].wait(), 10)
    finally:
        batch.cancel()
        with pytest.raises(asyncio.CancelledError):
            await batch
    receipts = [json.loads(path.read_text()) for path in workers.root.glob("worker-*/receipt.json")]
    assert len(receipts) == 2
    assert all(r["state"] == "uncertain" and r["cleanup"] == "unknown" for r in receipts)
    assert set(CapacityStore().holders("native")) == {r["candidate_id"] for r in receipts}
    for receipt in receipts:
        assert [h.session_id for h in LeaseStore().holders_of(receipt["workspace"])] == [receipt["candidate_id"]]
    assert (repo / "main.txt").read_text() == "original\n"
    assert not (repo / "a.txt").exists() and not (repo / "b.txt").exists()
    assert not runner.busy
    refused = await runner.run("editor", "retry", editing_worker=True)
    assert refused.metadata["refused"] == "worker.uncertain"


async def test_crashed_sibling_blocks_integration_of_ready_sibling(editing):
    crash = asyncio.Event()
    model = TaskScriptModel(barrier=asyncio.Barrier(2), waits={("alpha", 0): crash}, scripts={
        "alpha": [WorkerCrash("alpha crashed")],
        "beta": [_call("write_file", path="b.txt", content="beta\n"), _done()],
    })
    runner, workers, repo = _concurrent_workers(editing, model)
    alpha = asyncio.create_task(workers.run(runner, "editor", "alpha"))
    beta = asyncio.create_task(workers.run(runner, "editor", "beta"))
    try:
        ready = _receipt(await beta)
    finally:
        crash.set()
        with pytest.raises(WorkerCrash, match="alpha crashed"):
            await alpha
    assert (await _integrate(runner, workers, ready))["code"] == "worker.uncertain"
    refused = await workers.run(runner, "editor", "retry")
    assert refused.metadata["refused"] == "worker.uncertain"
    assert (repo / "main.txt").read_text() == "original\n" and not (repo / "b.txt").exists()


async def test_live_sibling_of_another_service_instance_blocks_admission(editing):
    release = asyncio.Event()
    model = TaskScriptModel(waits={("alpha", 0): release})
    runner, workers, repo = _concurrent_workers(editing, model)
    other = LocalEditingWorkers(workers.parent_guard, checks=list(workers.checks),
                                image=workers.image, root=workers.root, max_concurrent=2)
    live = asyncio.create_task(workers.run(runner, "editor", "alpha"))
    try:
        await asyncio.wait_for(model.reached["alpha", 0].wait(), 10)
        refused = await other.run(runner, "editor", "beta")
        assert refused.metadata["refused"] == "worker.uncertain"
        assert len(list(workers.root.glob("worker-*"))) == 1
    finally:
        release.set()
        _receipt(await live)


async def test_batch_respects_capacity_and_launch_budget(editing, monkeypatch):
    release, refused = asyncio.Event(), asyncio.Event()
    model = TaskScriptModel(waits={("alpha", 0): release, ("beta", 0): release})
    runner, workers, repo = _concurrent_workers(editing, model)
    capacity = CapacityStore()
    reservation = capacity.reserve("native", "other-worker", 2)

    def observe(event, store):
        if event["type"] == "editing_worker" and event["payload"].get("state") == "refused":
            refused.set()

    runner.events.add_observer(observe)
    batch = asyncio.create_task(runner.run_editing_workers([("editor", "alpha"), ("editor", "beta")]))
    try:
        await asyncio.wait_for(refused.wait(), 10)
    finally:
        release.set()
        results = await batch
        capacity.release(reservation)
    assert sorted(r.metadata["editing_worker"]["state"] for r in results) == ["ready", "refused"]
    assert [r.metadata["refused"] for r in results if not r.success] == ["worker.capacity_unavailable"]
    ready = _receipt(next(r for r in results if r.success))
    monkeypatch.setattr("garuda.workspace.worktrees._run_check", lambda *a: {"passed": True})
    assert (await _integrate(runner, workers, ready))["state"] == "integrated"
    assert len(set(model.calls)) == 1
    calls_before_refusal = list(model.calls)

    # Use a fresh store location so whole-batch refusal proves no admission at all.
    empty = LocalEditingWorkers(workers.parent_guard, checks=list(workers.checks), image=workers.image,
                                root=workers.root.parent / "unlaunched", max_concurrent=2)
    runner.editing_workers = empty
    runner.budget = DelegationBudget(max_launches=1)
    results = await runner.run_editing_workers([("editor", "alpha"), ("editor", "beta")])
    assert [r.metadata["refused"] for r in results] == ["agent.delegation_exhausted"] * 2
    assert runner.budget.launches == 0 and model.calls == calls_before_refusal
    assert not list(empty.root.glob("worker-*"))


@pytest.mark.parametrize("value", [0, True, 9, 1.5, "2"])
def test_worker_concurrency_requires_a_bounded_integer(editing, value):
    _, workers, _ = editing
    with pytest.raises(ValueError, match="max_concurrent"):
        LocalEditingWorkers(workers.parent_guard, checks=list(workers.checks),
                            image=workers.image, max_concurrent=value)


async def test_batch_child_exception_cancels_and_quarantines_siblings(editing):
    crash, blocked = asyncio.Event(), asyncio.Event()
    model = TaskScriptModel(barrier=asyncio.Barrier(2), waits={
        ("alpha", 0): crash, ("beta", 0): blocked,
    }, scripts={"alpha": [WorkerCrash("alpha crashed")], "beta": [_done()]})
    runner, workers, repo = _concurrent_workers(editing, model)
    batch = asyncio.create_task(runner.run_editing_workers([("editor", "alpha"), ("editor", "beta")]))
    try:
        for name in ("alpha", "beta"):
            await asyncio.wait_for(model.reached[name, 0].wait(), 10)
    finally:
        crash.set()
        with pytest.raises(BaseExceptionGroup) as failed:
            await batch
    assert len(failed.value.exceptions) == 1
    assert isinstance(failed.value.exceptions[0], WorkerCrash)
    receipts = [json.loads(path.read_text()) for path in workers.root.glob("worker-*/receipt.json")]
    assert len(receipts) == 2 and all(r["state"] == "uncertain" for r in receipts)
    assert set(CapacityStore().holders("native")) == {r["candidate_id"] for r in receipts}
    assert all(LeaseStore().holders_of(r["workspace"]) for r in receipts)
    assert not runner.busy and (repo / "main.txt").read_text() == "original\n"


@pytest.mark.parametrize("reason,code", [
    ("disabled", "worker.disabled"), ("busy", "agent.delegation_busy"),
    ("oversized", "worker.batch_invalid"), ("allowlist", "agent.subagent_not_allowed"),
    ("depth", "agent.delegation_too_deep"), ("launches", "agent.delegation_exhausted"),
    ("deadline", "agent.delegation_deadline"), ("empty", None),
])
async def test_batch_preflight_launches_nothing_on_any_refusal(editing, reason, code):
    model = TaskScriptModel()
    runner, workers, _ = _concurrent_workers(editing, model)
    requests = [("editor", "alpha"), ("editor", "beta")]
    if reason == "disabled":
        runner.editing_workers = None
    elif reason == "busy":
        runner.busy = True
    elif reason == "oversized":
        requests.append(("editor", "third"))
    elif reason == "allowlist":
        runner.allowed = ["editor"]
        requests[1] = ("foreign", "beta")
    elif reason == "depth":
        runner.depth = runner.budget.max_depth + 1
    elif reason == "launches":
        runner.budget.max_launches = 1
    elif reason == "deadline":
        runner.deadline_monotonic = 0
    else:
        requests = []
    results = await runner.run_editing_workers(requests)
    assert [r.metadata["refused"] for r in results] == [code] * len(requests)
    assert len({id(result) for result in results}) == len(results)
    assert runner.budget.launches == 0 and not model.calls
    assert not list(workers.root.glob("worker-*"))


@pytest.mark.parametrize("native_ceiling", [1, 3])
async def test_batch_counts_parent_slot_against_configured_native_ceiling(editing, native_ceiling):
    runner, previous, repo = editing
    Path(os.environ["GARUDA_GLOBAL_SETTINGS"]).write_text(f"capacity:\n  native: {native_ceiling}\n")
    await previous.parent_guard.release()
    parent = WorkspaceLeaseGuard(str(repo), runner.events.session_id, capacity_key="native")
    parent.acquire()
    workers = LocalEditingWorkers(parent, checks=list(previous.checks), image=previous.image,
                                 root=previous.root, max_concurrent=2)
    runner.editing_workers = workers
    model = runner.model = TaskScriptModel(barrier=asyncio.Barrier(2))
    try:
        results = await runner.run_editing_workers([("editor", "alpha"), ("editor", "beta")])
        assert CapacityStore().holders("native") == [runner.events.session_id]
        if native_ceiling == 1:
            assert [r.metadata["refused"] for r in results] == ["worker.capacity_unavailable"] * 2
            assert not model.calls
        else:
            assert all(_receipt(r)["state"] == "ready" for r in results)
        assert (repo / "main.txt").read_text() == "original\n"
    finally:
        await parent.release()


async def test_concurrent_worker_deltas_use_candidate_ids(editing):
    answers = {name: f"{name} worker answer" for name in ("alpha", "beta")}
    scripts = {
        name: [
            _call("write_file", path=f"{name[0]}.txt", content=f"{name}\n"),
            ScriptedStream([
                StreamDelta(content_delta=name + " "),
                StreamDelta(content_delta="worker answer"),
                StreamDelta(tool_call_delta={
                    "index": 0, "id": "done", "name": "task_complete",
                    "arguments": json.dumps(_done().tool_calls[0].arguments),
                }),
                StreamDelta(done=True, finish_reason="tool_calls"),
            ]),
        ] for name in answers
    }
    runner, _, _ = _concurrent_workers(
        editing, TaskScriptModel(barrier=asyncio.Barrier(2), scripts=scripts),
    )
    seen = []
    both_streaming = asyncio.Barrier(2)

    async def consume(delta):
        seen.append(delta)
        if delta.kind == "text" and delta.text in {"alpha ", "beta "}:
            await asyncio.wait_for(both_streaming.wait(), 10)

    runner.parent_delta_consumer = consume
    results = await runner.run_editing_workers([("editor", name) for name in answers])
    ids = {result.metadata["editing_worker"]["candidate_id"] for result in results}
    assert {d.source for d in seen} == ids
    for result, answer in zip(results, answers.values(), strict=True):
        source = result.metadata["editing_worker"]["candidate_id"]
        assert result.success
        assert source == result.metadata["subagent_session_id"]
        fragments = [d for d in seen if d.source == source]
        assert "".join(d.text for d in fragments if d.kind == "text") == answer
        assert {(d.agent, d.depth) for d in fragments} == {("editor", 1)}


class FanoutModel(TaskScriptModel):
    """The parent emits one batch; child scripts are selected by child task."""

    def __init__(self, *, tasks=None, integrate="none", **kwargs):
        super().__init__(**kwargs)
        self.batch = {"tasks": tasks or [{"profile": "editor", "task": name}
                                       for name in ("alpha", "beta")], "integrate": integrate}
        self.parent_called = False
        self.child_tool_names = []

    async def complete(self, messages, **kwargs):
        if any(m.role == Role.USER and m.content == "delegate requested edits" for m in messages):
            if not self.parent_called:
                self.parent_called = True
                assert "fan_out_editing_workers" in {t["function"]["name"] for t in kwargs["tools"]}
                return _call("fan_out_editing_workers", **self.batch)
            return ModelResponse(content="parent finished", tool_calls=[])
        if not kwargs.get("tools"):
            return ModelResponse(content='{"criteria": []}', tool_calls=[])
        self.child_tool_names.append({t["function"]["name"] for t in kwargs["tools"]})
        return await super().complete(messages, **kwargs)


async def _model_fanout(editing, model, **config_options):
    runner, workers, repo = _concurrent_workers(editing, model)
    config = AgentConfig(enable_verifier=False, enable_acceptance_contract=False,
                         max_turns=3, editing_workers=workers, editing_worker_fanout=True,
                         delegation_budget=runner.budget, **config_options)
    result = await DefaultAgent().run(
        "delegate requested edits", model, runner.env, default_tools(), config=config,
        events=runner.events, permissions=runner.parent_permissions, agents_dir=runner.agents_dir,
    )
    message = next(m for m in result.messages
                   if m.role == Role.TOOL and m.name == "fan_out_editing_workers")
    return json.loads(message.content), message, runner, workers, repo


@pytest.mark.parametrize("rule", ["deny", "ask"])
async def test_fanout_delegation_tool_refusal_launches_no_workers(editing, rule):
    from garuda.core.editing_fanout import run_fanout
    from garuda.interfaces.run_guard import install_session_broker

    runner, workers, _ = _concurrent_workers(editing, TaskScriptModel())
    asks = []

    async def approve(action):
        asks.append(action)
        # Refuse the second task: even an approved first task must not launch.
        return len(asks) == 1

    permissions = PermissionEngine(tool_rules={"invoke_subagent": rule}, approval_handler=approve)
    install_session_broker(permissions, None, runner.events.session_id)
    payload, error = await run_fanout(
        runner, {"tasks": [{"profile": "editor", "task": n} for n in ("alpha", "beta")]},
        env=runner.env, session_id=runner.events.session_id, permissions=permissions,
        output_budget=16_000,
    )
    assert error and payload["code"] == "fanout.delegation_not_admitted"
    assert runner.budget.launches == 0
    assert not list(workers.root.glob("worker-*"))
    if rule == "ask":
        assert len(asks) == 2
        assert all("invoke_subagent(" in action and "'editing_worker': True" in action for action in asks)
        assert "alpha" in asks[0] and "beta" in asks[1]
    else:
        assert asks == []


@pytest.mark.parametrize("rule", ["deny", "ask"])
async def test_fanout_integration_tool_refusal_preserves_ready_candidate(editing, monkeypatch, rule):
    from garuda.core.editing_fanout import run_fanout
    from garuda.interfaces.run_guard import install_session_broker

    runner, workers, repo = _concurrent_workers(editing, TaskScriptModel())
    asks, checked = [], []

    async def approve(action):
        asks.append(action)
        return False

    def check(*args):
        checked.append(args)
        return {"passed": True}

    monkeypatch.setattr("garuda.workspace.worktrees._run_check", check)
    permissions = PermissionEngine(tool_rules={"integrate_editing_worker": rule},
                                   approval_handler=approve)
    install_session_broker(permissions, None, runner.events.session_id)
    payload, error = await run_fanout(
        runner, {"tasks": [{"profile": "editor", "task": "alpha"}], "integrate": "in_order"},
        env=runner.env, session_id=runner.events.session_id,
        permissions=permissions, output_budget=16_000,
    )
    entry, = payload["workers"]
    assert error and entry["outcome"] == "integration_refused"
    assert entry["integration"]["code"] == "integration.permission_denied"
    assert "state" not in entry
    assert checked == []
    assert not (repo / "a.txt").exists()
    assert json.loads((Path(entry["workspace"]).parent / "receipt.json").read_text())["state"] == "ready"
    if rule == "ask":
        assert len(asks) == 1
        assert "integrate_editing_worker(" in asks[0]
        assert entry["candidate_id"] in asks[0] and entry["provenance_digest"] in asks[0]
    else:
        assert asks == []


async def test_model_fanout_runs_workers_concurrently_and_returns_ordered_receipts(editing):
    model = FanoutModel(barrier=asyncio.Barrier(2))
    payload, message, runner, workers, repo = await _model_fanout(editing, model)
    assert payload["version"] == 1 and payload["state"] == "completed"
    assert [w["index"] for w in payload["workers"]] == [0, 1]
    assert [w["outcome"] for w in payload["workers"]] == ["ready", "ready"]
    for entry, name in zip(payload["workers"], ("alpha", "beta"), strict=True):
        assert entry["child_result"]["outcome"]["validation"] == "accepted"
        assert entry["child_result"]["output"] == {"edited": True}
        assert Path(entry["workspace"], f"{name[0]}.txt").read_text() == f"{name}\n"
    assert all("fan_out_editing_workers" not in names for names in model.child_tool_names)
    assert runner.budget.launches == 2
    assert (repo / "main.txt").read_text() == "original\n"
    assert not (repo / "a.txt").exists() and not (repo / "b.txt").exists()
    assert "[buffer:" not in message.content


async def test_model_fanout_in_order_integration_marks_overlapping_sibling_stale(editing, monkeypatch):
    from garuda.interfaces.run_guard import install_session_broker

    runner, _, repo = editing
    asks = []

    async def approve(action):
        asks.append(action)
        return True

    runner.parent_permissions = PermissionEngine(
        workspace_root=repo, approval_handler=approve,
        tool_rules={"invoke_subagent": "ask", "integrate_editing_worker": "ask"},
    )
    install_session_broker(runner.parent_permissions, None, runner.events.session_id)
    monkeypatch.setattr("garuda.workspace.worktrees._run_check", lambda *a: {"passed": True})
    model = FanoutModel(barrier=asyncio.Barrier(2), integrate="in_order", scripts={
        name: [_call("write_file", path="main.txt", content=f"{name}\n"), _done()]
        for name in ("alpha", "beta")
    })
    payload, _, _, _, repo = await _model_fanout(editing, model)
    assert [w["outcome"] for w in payload["workers"]] == ["integrated", "stale_base"]
    assert all("state" not in w for w in payload["workers"])
    assert payload["workers"][1]["integration"]["code"] == "integration.stale_base"
    assert [action.split("(", 1)[0] for action in asks] == [
        "invoke_subagent", "invoke_subagent", "integrate_editing_worker", "integrate_editing_worker",
    ]
    for action, entry in zip(asks[2:], payload["workers"], strict=True):
        assert entry["candidate_id"] in action and entry["provenance_digest"] in action
    assert (repo / "main.txt").read_text() == "alpha\n"


async def test_model_fanout_integration_respects_parent_path_denial(editing, monkeypatch):
    runner, _, repo = editing
    runner.parent_permissions = PermissionEngine(workspace_root=repo,
                                                path_rules={"deny": [str(repo / "main.txt")]})
    monkeypatch.setattr("garuda.workspace.worktrees._run_check", lambda *a: {"passed": True})
    model = FanoutModel(tasks=[{"profile": "editor", "task": "alpha"}], integrate="in_order",
                        scripts={"alpha": [_call("write_file", path="main.txt", content="denied\n"), _done()]})
    payload, _, _, _, _ = await _model_fanout(editing, model)
    assert payload["workers"][0]["outcome"] == "failed"
    assert "integration" not in payload["workers"][0]
    assert (repo / "main.txt").read_text() == "original\n"


async def test_model_fanout_child_exception_quarantines_and_integrates_nothing(editing, monkeypatch):
    # Ordinary model errors are caught by the loop. Crash the real child's
    # environment settlement so TaskGroup observes an ordinary escaped exception.
    model = FanoutModel(barrier=asyncio.Barrier(2), integrate="in_order",
                        waits={("beta", 0): asyncio.Event()})
    close = LocalEnvironment.aclose

    async def crash_child(environment):
        await close(environment)
        if (environment.workspace_root / "a.txt").exists():
            raise RuntimeError("child close failed")

    monkeypatch.setattr(LocalEnvironment, "aclose", crash_child)
    payload, _, runner, workers, repo = await _model_fanout(editing, model)
    assert payload["code"] == "worker.batch_failed"
    receipts = [json.loads(p.read_text()) for p in workers.root.glob("worker-*/receipt.json")]
    assert len(receipts) == 2 and all(r["state"] == "uncertain" for r in receipts)
    assert (repo / "main.txt").read_text() == "original\n" and not (repo / "a.txt").exists()
    assert not runner.busy


@pytest.mark.parametrize("reason,code", [
    ("oversized", "worker.batch_invalid"), ("allowlist", "agent.subagent_not_allowed"),
    ("launches", "agent.delegation_exhausted"), ("deadline", "agent.delegation_deadline"),
])
async def test_model_fanout_oversized_batch_from_model_launches_nothing(editing, reason, code):
    runner, _, _ = editing
    tasks = [{"profile": "editor", "task": name} for name in ("alpha", "beta")]
    options = {}
    if reason == "oversized":
        tasks.append({"profile": "editor", "task": "third"})
    elif reason == "allowlist":
        options["subagents"] = ["editor"]
        tasks[1]["profile"] = "foreign"
    elif reason == "launches":
        runner.budget.max_launches = 1
    else:
        options["deadline_sec"] = 0.000001
    payload, _, runner, workers, _ = await _model_fanout(editing, FanoutModel(tasks=tasks), **options)
    assert payload["code"] == code
    assert runner.budget.launches == 0
    assert not list(workers.root.glob("worker-*"))


async def test_model_fanout_refuses_non_local_environment(editing):
    from garuda.core.editing_fanout import run_fanout
    from garuda.workspace.health import HealthMonitoredEnvironment

    runner, workers, repo = _concurrent_workers(editing, TaskScriptModel())
    # Exercise a direct call: shared assembly correctly hides the tool for a
    # non-local config. A monitored foreign backend must still refuse at call time.
    class ForeignEnvironment:
        workspace_root = repo

    runner.env = HealthMonitoredEnvironment(ForeignEnvironment())
    payload, error = await run_fanout(runner, {"tasks": [{"profile": "editor", "task": "alpha"}]},
                                     env=runner.env, session_id=runner.events.session_id,
                                     permissions=runner.parent_permissions, output_budget=16_000)
    assert error and payload["code"] == "worker.unsupported_environment"
    assert runner.budget.launches == 0
    assert not list(workers.root.glob("worker-*"))


async def test_model_fanout_children_share_run_tree_spend_gate(editing):
    from garuda.core.spend import SpendGate

    gate = SpendGate(max_tokens=1_000_000)
    payload, _, _, _, _ = await _model_fanout(editing, FanoutModel(barrier=asyncio.Barrier(2)),
                                            spend_gate=gate)
    assert all(w["outcome"] == "ready" for w in payload["workers"])
    assert gate.calls >= 6 and gate.settled_tokens > 0
    assert gate.reserved_tokens == gate.reserved_calls == 0


@pytest.mark.parametrize("output_limit,buffer_threshold", [(1800, 30_720), (30_720, 1800)])
async def test_model_fanout_output_fits_live_budget(editing, output_limit, buffer_threshold):
    payload, message, _, _, _ = await _model_fanout(
        editing, FanoutModel(barrier=asyncio.Barrier(2)), max_output_bytes=output_limit,
        buffer_threshold_bytes=buffer_threshold,
    )
    assert len(message.content.encode("utf-8")) <= min(output_limit, buffer_threshold)
    for worker in payload["workers"]:
        assert worker["outcome"] == "ready" and worker["candidate_id"]
        assert worker["child_result"]["delivery"]["state"] in {"unavailable", "referenced"}


async def test_fanout_cancellation_propagates_and_quarantines_live_children(editing):
    from garuda.core.editing_fanout import run_fanout

    blocked = asyncio.Event()
    model = TaskScriptModel(barrier=asyncio.Barrier(2), waits={
        ("alpha", 0): blocked, ("beta", 0): blocked,
    })
    runner, workers, repo = _concurrent_workers(editing, model)
    task = asyncio.create_task(run_fanout(
        runner, {"tasks": [{"profile": "editor", "task": n} for n in ("alpha", "beta")],
                 "integrate": "in_order"}, env=runner.env,
        session_id=runner.events.session_id, permissions=runner.parent_permissions, output_budget=16_000,
    ))
    try:
        for name in ("alpha", "beta"):
            await asyncio.wait_for(model.reached[name, 0].wait(), 10)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    receipts = [json.loads(p.read_text()) for p in workers.root.glob("worker-*/receipt.json")]
    assert len(receipts) == 2 and all(r["state"] == "uncertain" for r in receipts)
    assert (repo / "main.txt").read_text() == "original\n"


async def test_model_fanout_uncertain_integration_halts_later_candidates(editing, monkeypatch):
    monkeypatch.setattr("garuda.workspace.worktrees._run_check", lambda *a: {"passed": True})

    def interrupted_apply(*args, **kwargs):
        raise OSError("cannot prove patch application")

    monkeypatch.setattr("garuda.core.editing_worker.apply_snapshot_patch", interrupted_apply)
    payload, _, _, workers, repo = await _model_fanout(
        editing, FanoutModel(barrier=asyncio.Barrier(2), integrate="in_order"),
    )
    assert [w["outcome"] for w in payload["workers"]] == ["quarantined", "ready"]
    assert "state" not in payload["workers"][0]
    assert payload["workers"][1]["integration"] == {
        "state": "skipped", "code": "fanout.halted_after_uncertain",
    }
    assert (repo / "main.txt").read_text() == "original\n"
    assert not (repo / "a.txt").exists() and not (repo / "b.txt").exists()
    assert sorted(json.loads(p.read_text())["state"]
                  for p in workers.root.glob("worker-*/receipt.json")) == ["ready", "uncertain"]


async def test_model_fanout_large_typed_output_uses_parent_retrieval(editing):
    result = {"edited": True, "details": "λ" * 10_000}
    done = _call("task_complete", summary="Updated the requested file in the isolated worker.",
                 result=result, verification_commands=["test -s main.txt"])
    payload, _, _, _, _ = await _model_fanout(editing, FanoutModel(scripts={
        name: [_call("write_file", path=f"{name[0]}.txt", content=f"{name}\n"), done]
        for name in ("alpha", "beta")
    }))
    from garuda.core.buffer import ToolOutputBuffer
    from garuda.core.child_result import read_child_result_reference

    for entry in payload["workers"]:
        envelope = entry["child_result"]
        assert envelope["outcome"]["validation"] == "accepted"
        assert envelope["delivery"]["state"] == "referenced"
        ref = envelope["delivery"]["reference"]
        storage = ToolOutputBuffer(session_id=ref["parent_session_id"])
        assert read_child_result_reference(ref, storage=storage)[0] == result


@pytest.mark.parametrize("denial", ["path", "acceptance"])
async def test_model_fanout_integration_rechecks_parent_permission_after_children_settle(editing, denial):
    runner, _, repo = editing
    settled = False
    asks = []

    async def approve(action):
        asks.append((action, settled))
        return not settled

    runner.parent_permissions = PermissionEngine(
        workspace_root=repo, approval_handler=approve,
        path_rules={"ask": [str(repo / "main.txt")]} if denial == "path" else None,
        bash_rules={"ask": [r"^test -s main.txt$"]} if denial == "acceptance" else None,
    )

    def observe(event, store):
        nonlocal settled
        if (event["type"] == "editing_worker" and event["payload"].get("state") == "ready"
                and event["payload"].get("child_settled")):
            settled = True

    runner.events.add_observer(observe)
    model = FanoutModel(tasks=[{"profile": "editor", "task": "alpha"}], integrate="in_order",
                        scripts={"alpha": [_call("write_file", path="main.txt", content="alpha\n"), _done()]})
    payload, _, _, _, _ = await _model_fanout(editing, model)
    entry, = payload["workers"]
    assert entry["child_result"]["outcome"]["validation"] == "accepted"
    assert entry["outcome"] == "integration_refused"
    assert entry["integration"]["code"] == "integration.permission_denied"
    assert asks[-1][1] is True
    assert (repo / "main.txt").read_text() == "original\n"


async def test_model_fanout_uncertain_child_halts_before_ready_sibling(editing):
    class CrashingChild(FanoutModel):
        async def complete(self, messages, **kwargs):
            if kwargs.get("tools") and any(m.role == Role.USER and m.content == "alpha" for m in messages):
                raise RuntimeError("ordinary child model failure")
            return await super().complete(messages, **kwargs)

    payload, _, _, _, repo = await _model_fanout(editing, CrashingChild(integrate="in_order"))
    assert [w["outcome"] for w in payload["workers"]] == ["quarantined", "ready"]
    assert payload["workers"][1]["integration"]["code"] == "fanout.halted_after_uncertain"
    assert not (repo / "b.txt").exists()
