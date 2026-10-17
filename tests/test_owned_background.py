"""Background dashboard admission at the detached worker and queue boundary."""

from __future__ import annotations

import asyncio
import os
import sys
import uuid
from pathlib import Path

import pytest
import yaml

from garuda.config import garuda_yaml as gy
from garuda.interfaces import bg_sessions
from garuda.runtime.capacity import CapacityStore
from garuda.runtime.queue import QueueStore, scope_for
from garuda.scenarios.gateway import FlowStartGateway, FlowStartRequest
from tests.test_web_flow_starts import LINUX, _body, _call
from tests.test_web_run_starts import _context, _preview, _start, _workspace


def configure(tmp_path, *, parked=False):
    fixture = tmp_path / "fixture.py"
    counter = tmp_path / "prompts"
    fixture.write_text(
        "import sys,pathlib\n"
        f"sys.path.insert(0,{str(Path(__file__).resolve().parents[1])!r})\n"
        "from garuda.acp import fake_agent\n"
        "original=fake_agent._handle_prompt\n"
        "def prompt(*args,**kwargs):\n"
        f"    with pathlib.Path({str(counter)!r}).open('a') as output: output.write('prompt\\n')\n"
        "    return original(*args,**kwargs)\n"
        "fake_agent._handle_prompt=prompt\n"
        f"raise SystemExit(fake_agent.main(['--profile',{'approval' if parked else 'success'!r}]))\n")
    settings = Path(os.environ["GARUDA_GLOBAL_SETTINGS"])
    settings.write_text(yaml.safe_dump({"runtimes": [{"runtime_id": "background-fixture", "kind": "acp",
        "version": "1", "command": [sys.executable, str(fixture)]}], "capacity": {"background-fixture": 1}}))
    gy.user_path().write_text(gy.dump({"version": 1, "roles": {"coder": {"harness": "background-fixture"}}}))
    return counter


async def wait_for(call, predicate, timeout=30):
    async with asyncio.timeout(timeout):
        while True:
            value = await call()
            if predicate(value):
                return value
            await asyncio.sleep(0.05)


async def terminal(gateway, operation):
    return await wait_for(lambda: gateway.status(operation),
                          lambda row: row["status"] in {"terminal", "quarantined"})


@LINUX
@pytest.mark.parametrize("action", ["finish", "cancel", "answer"])
async def test_detached_start_survives_server_close_and_duplicate_without_second_prompt(tmp_path, action):
    workspace = _workspace(tmp_path)
    counter = configure(tmp_path, parked=action != "finish")
    ctx = await _context(tmp_path, workspace)
    replacement = None
    session = None
    try:
        inputs = {"goal": "execute exactly once in the detached worker", "options": {"bg": True}}
        preview = await _preview(ctx, "run-with-role", inputs)
        assert preview["authority"]["can_start"], preview["authority"]["diagnostics"]
        operation = str(uuid.uuid4())
        request = _start(preview, operation, "run-with-role", inputs)
        response = await _call(ctx, "/api/scenarios/start", method="POST", body=request)
        assert response.status in {201, 202}, _body(response)
        session = _body(response)["session_id"]
        await ctx.flow_starts.aclose()
        replacement = FlowStartGateway(store=ctx.store, workspaces=ctx.workspaces,
            max_permission="smart", approvals=ctx.flow_starts.approvals)
        # No compile, enqueue or spawn path is reachable through this duplicate.
        duplicate = await replacement.admit(FlowStartRequest.parse(request))
        assert duplicate["duplicate"] and duplicate["session_id"] == session
        if action != "finish":
            from garuda.acp.approval_channel import FileApprovalChannel

            async def pending():
                for child in ctx.store.list_sessions():
                    if child.get("flow_step", {}).get("flow_session") == session:
                        channel = FileApprovalChannel(ctx.store.session_dir(child["session_id"]) / "approvals", child["session_id"])
                        rows = channel.pending()
                        if rows:
                            return channel, rows[0]
                return None

            channel, approval = await wait_for(pending, bool)
            assert CapacityStore().holders("background-fixture") == [session]
            if action == "cancel":
                assert bg_sessions.cancel(ctx.store, session) == "asked the running worker to stop"
            else:
                from garuda.acp.approval_channel import write_answer

                write_answer(channel.directory, channel.session_id, approval["approval_id"], True,
                             expect_digest=approval["digest"])
        result = await terminal(replacement, operation)
        assert result["status"] == "terminal", result
        assert result["outcome"] == ("interrupted" if action == "cancel" else "completed"), result
        assert counter.read_text().splitlines() == ["prompt"]
        assert QueueStore().entries() == []
        assert CapacityStore().holders("background-fixture") == []
        meta = ctx.store.load_meta(session)
        assert meta["background_closure"]["queue_released"]
        assert meta["flow_operation"]["operation_id"] == operation
        assert replacement.intents.read(operation)["phase"] == "launch-authorized"
        again = await replacement.admit(FlowStartRequest.parse(request))
        assert again["session_id"] == session and counter.read_text().splitlines() == ["prompt"]
    finally:
        if session:
            bg_sessions.cancel(ctx.store, session)
        await ctx.flow_starts.aclose()
        if replacement:
            await replacement.aclose()
        await ctx.live.aclose()


@LINUX
@pytest.mark.parametrize("action", ["cancel", "drift"])
async def test_waiting_background_holds_no_workspace_and_never_dispatches_after_cancel_or_drift(tmp_path, action):
    from garuda.workspace.lease import LeaseStore

    workspace = _workspace(tmp_path)
    counter = configure(tmp_path)
    capacity = CapacityStore()
    blocker = capacity.reserve("background-fixture", "foreground-blocker", 1)
    ctx = await _context(tmp_path, workspace)
    session = None
    try:
        inputs = {"goal": "wait without a workspace lease", "sources": ["source.txt"],
                  "options": {"bg": True, "isolation": "worktree"}}
        preview = await _preview(ctx, "run-with-role", inputs)
        operation = str(uuid.uuid4())
        response = await _call(ctx, "/api/scenarios/start", method="POST", body=_start(
            preview, operation, "run-with-role", inputs))
        assert response.status in {201, 202}, _body(response)
        session = _body(response)["session_id"]
        assert (await ctx.flow_starts.status(operation))["status"] == "queued"
        assert LeaseStore().live_holders_for_session(session) == []
        assert not ctx.store.load_meta(session).get("worktree")
        if action == "cancel":
            assert bg_sessions.cancel(ctx.store, session) == "removed from the queue before it started"
        else:
            (workspace / "source.txt").write_text("changed while queued\n")
        capacity.release(blocker)
        result = await terminal(ctx.flow_starts, operation)
        assert result["status"] == "terminal", result
        assert result["outcome"] == ("interrupted" if action == "cancel" else "stopped")
        assert not counter.exists()
        assert QueueStore().entries(scope_for("background-fixture")) == []
        assert capacity.holders("background-fixture") == []
    finally:
        capacity.release(blocker)
        if session:
            bg_sessions.cancel(ctx.store, session)
        await ctx.flow_starts.aclose()
        await ctx.live.aclose()


@LINUX
@pytest.mark.parametrize("isolation,checked", [("worktree", True), ("auto", False)])
async def test_detached_native_run_keeps_workspace_metadata_and_owned_checks(tmp_path, monkeypatch, isolation, checked):
    from tests.test_web_flow_starts import _local_model
    from tests.test_web_run_starts import _configure

    monkeypatch.setenv("OPENAI_API_KEY", "local-protocol-fixture")
    workspace = _workspace(tmp_path)
    _configure(workspace)
    server, requests, _started, _release = _local_model()
    Path(os.environ["GARUDA_GLOBAL_SETTINGS"]).write_text(yaml.safe_dump({
        "models": {"owned": {"model": "openai/owned-start",
                              "api_base": f"http://127.0.0.1:{server.server_port}/v1"}},
        "model_bindings": {"default": {"reasoning": "owned"}}, "capacity": {"native": 1}}))
    ctx = await _context(tmp_path, workspace)
    session = None
    try:
        inputs = {"goal": "finish one detached native run",
                  "options": {"bg": True, "isolation": isolation,
                              **({"checks": ["git status --porcelain"]} if checked else {})}}
        preview = await _preview(ctx, "run-with-role", inputs)
        operation = str(uuid.uuid4())
        response = await _call(ctx, "/api/scenarios/start", method="POST", body=_start(
            preview, operation, "run-with-role", inputs))
        assert response.status in {201, 202}, _body(response)
        session = _body(response)["session_id"]
        result = await terminal(ctx.flow_starts, operation)
        assert result["status"] == "terminal" and result["outcome"] == "completed", result
        assert len(requests) == 1
        meta = ctx.store.load_meta(session)
        if isolation == "worktree":
            assert meta["workspace"] == meta["worktree"] != str(workspace)
            assert meta["source_repo"] == str(workspace)
        assert bool(meta.get("acceptance_receipts")) is checked
        assert meta["background_closure"]["workspace_released"]
        assert CapacityStore().holders("native") == []
    finally:
        if session and ctx.store.session_dir(session).joinpath("meta.json").exists():
            bg_sessions.cancel(ctx.store, session)
        await ctx.flow_starts.aclose()
        await ctx.live.aclose()
        server.shutdown()
        server.server_close()


@pytest.mark.parametrize("cut,status", [("enqueue", "uncertain"), ("spawn", "terminal")])
async def test_partial_background_publication_is_projected_without_reenqueue(tmp_path, monkeypatch, cut, status):
    workspace = _workspace(tmp_path)
    counter = configure(tmp_path)
    monkeypatch.setattr(sys, "platform", "linux")
    ctx = await _context(tmp_path, workspace)
    calls = []
    original = QueueStore.enqueue

    def enqueue(queue, *args, **kwargs):
        calls.append("enqueue")
        if cut == "enqueue":
            raise OSError("publication cut before queue admission")
        return original(queue, *args, **kwargs)

    def spawn(*args, **kwargs):
        calls.append("spawn")
        raise OSError("worker could not be created")

    monkeypatch.setattr(QueueStore, "enqueue", enqueue)
    monkeypatch.setattr(bg_sessions, "_spawn_worker", spawn)
    try:
        inputs = {"goal": "never retry a partial detached launch", "options": {"bg": True}}
        preview = await _preview(ctx, "run-with-role", inputs)
        operation = str(uuid.uuid4())
        request = _start(preview, operation, "run-with-role", inputs)
        response = await _call(ctx, "/api/scenarios/start", method="POST", body=request)
        assert _body(response)["status"] == status
        await ctx.flow_starts.aclose()
        replacement = FlowStartGateway(store=ctx.store, workspaces=ctx.workspaces,
                                      max_permission="smart", approvals=ctx.flow_starts.approvals)
        before = list(calls)
        row = await replacement.admit(FlowStartRequest.parse(request))
        assert row["status"] == status and row["duplicate"]
        assert calls == before and not counter.exists()
        assert QueueStore().entries() == []
        assert replacement.intents.read(operation)["queue_admission"]["item_id"] == row["session_id"]
        await replacement.aclose()
    finally:
        await ctx.flow_starts.aclose()
        await ctx.live.aclose()


async def test_queue_coordinator_cannot_release_an_active_owned_role_loan(tmp_path):
    from garuda.interfaces.run_guard import BorrowedLease, WorkspaceLeaseGuard
    from garuda.runtime.ownership import current_owner
    from garuda.scenarios.background import BackgroundAdmission

    capacity = CapacityStore(tmp_path / "capacity")
    queue = QueueStore(tmp_path / "queue", capacity=capacity, ceiling=lambda _key: 1)
    session, child = str(uuid.uuid4()), str(uuid.uuid4())
    scope, owner = scope_for("native"), current_owner()
    queue.enqueue(scope, session, harness="native", session_id=session, config_digest="a" * 64)
    assert queue.try_claim(scope, session, owner)
    queue.begin_dispatch(scope, session, owner)
    admission = BackgroundAdmission(queue, session, scope, owner, {"queue": {
        "scope": scope, "item_id": session, "harness": "native", "config_digest": "a" * 64}})
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    parent = WorkspaceLeaseGuard(str(workspace), session, capacity_key="native", queued_admission=admission)
    parent.acquire()
    capability = parent.delegate(child)
    borrower = BorrowedLease(capability, capacity_key="native")
    borrower.acquire()
    try:
        from garuda.runtime.capacity import CapacityError

        with pytest.raises(CapacityError, match="borrower"):
            queue.release(scope, session, owner)
        assert capacity.holders("native") == [session]
        assert queue.entries(scope)[0]["quarantined"]  # failed release keeps its durable intent
    finally:
        await borrower.release()
        parent.revoke(capability)
        await parent.release()
    assert queue.release(scope, session, owner)
    assert capacity.holders("native") == [] and queue.entries(scope) == []
