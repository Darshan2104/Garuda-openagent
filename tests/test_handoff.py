"""Handoff transaction tests for issue #18 (P0.10).

Every transition, rollback branch, cancellation point, and restart state:
success transfers the single ownership, target failure keeps the source
resumable, cancellation lands recoverable, and each move emits a typed event.
"""

import pytest

from garuda.runtime.events import RuntimeEventKind
from garuda.runtime.fake import FakeRuntime, FakeScenario
from garuda.runtime.handoff import (
    HandoffError,
    HandoffPhase,
    HandoffTransaction,
    execute_handoff,
)
from garuda.runtime.protocol import LifecycleState


def _started(scenario=FakeScenario.SUCCESS, session_id="s"):
    async def _make():
        rt = FakeRuntime(scenario, runtime_id=f"fake-{session_id}")
        await rt.start(task="work", session_id=session_id)
        return rt

    return _make


async def _begin(tx, source, **kwargs):
    kwargs.setdefault("checkpoint", lambda: None)
    await tx.begin(source, **kwargs)


async def test_success_transfers_single_ownership_with_ordered_events():
    source = await _started()()
    target = FakeRuntime(FakeScenario.SUCCESS, runtime_id="fake-target")
    seen: list = []
    tx = HandoffTransaction(session_id="s", emit=seen.append)
    await _begin(tx, source, capture=lambda: {"diff": "M a.py"})
    assert tx.captured == {"diff": "M a.py"}
    await tx.start_target(source, target)
    await tx.acknowledge(source, target)

    assert tx.phase is HandoffPhase.ACKNOWLEDGED
    assert source.state is LifecycleState.CLOSED
    assert target.state is LifecycleState.IDLE
    phases = [e.payload["handoff_phase"] for e in seen]
    assert phases == [
        "pausing",
        "checkpointing",
        "capturing",
        "generating",
        "starting_target",
        "awaiting_ack",
        "acknowledged",
    ]
    assert all(e.kind is RuntimeEventKind.LIFECYCLE for e in seen)
    assert [e.seq for e in seen] == [1, 2, 3, 4, 5, 6, 7]


async def test_target_failure_keeps_source_resumable():
    source = await _started()()
    target = FakeRuntime(FakeScenario.STARTUP_FAILURE, runtime_id="fake-target")
    tx = HandoffTransaction(session_id="s")
    await _begin(tx, source)
    with pytest.raises(HandoffError, match="target startup failed"):
        await tx.start_target(source, target)
    assert tx.phase is HandoffPhase.ROLLED_BACK
    assert source.state is LifecycleState.IDLE
    turn = await source.prompt("still here")
    assert turn == 1


async def test_cancel_mid_switch_is_recoverable():
    source = await _started()()
    target = FakeRuntime(FakeScenario.SUCCESS, runtime_id="fake-target")
    tx = HandoffTransaction(session_id="s")
    await _begin(tx, source)
    await tx.start_target(source, target)
    await tx.cancel(source, target, reason="user stopped")
    assert tx.phase is HandoffPhase.CANCELLED
    assert source.state is LifecycleState.IDLE
    assert target.state is LifecycleState.CLOSED
    with pytest.raises(HandoffError):
        await tx.acknowledge(source, target)


async def test_acknowledge_refuses_two_mutating_owners():
    source = await _started()()
    target = FakeRuntime(FakeScenario.SUCCESS, runtime_id="fake-target")
    tx = HandoffTransaction(session_id="s")
    await _begin(tx, source)
    await tx.start_target(source, target)
    await source.resume_from_pause()
    assert source.state is LifecycleState.IDLE
    with pytest.raises(HandoffError, match="two active mutating owners"):
        await tx.acknowledge(source, target)
    assert source.state is LifecycleState.IDLE
    assert target.state is LifecycleState.CLOSED


async def test_delivery_runs_on_the_target_only_after_acknowledgement(tmp_path):
    """The package is the new owner's first prompt: by the time it runs,
    ownership and the target segment are persisted and the source is closed."""
    from garuda.core.sessions import SessionStore

    store = SessionStore(tmp_path / "sessions")
    source = await _native_source(tmp_path, store, "deliver-after-ack")
    target = FakeRuntime(FakeScenario.SUCCESS, runtime_id="fake-target")
    observed: dict = {}

    async def deliver(runtime):
        unified = store.load_unified("deliver-after-ack")
        observed["handoff"] = unified.handoff["state"]
        observed["active"] = unified.active.runtime_id
        observed["source"] = source.state
        await runtime.prompt("handoff package")

    tx, returned = await execute_handoff(
        session_id="deliver-after-ack",
        source=source,
        target_factory=lambda: target,
        store=store,
        deliver=deliver,
    )
    assert observed == {
        "handoff": "acknowledged",
        "active": "fake-target",
        "source": LifecycleState.CLOSED,
    }
    assert tx.phase is HandoffPhase.ACKNOWLEDGED
    assert returned is target
    assert target.state is LifecycleState.IDLE
    events, _ = await target.poll_events(0)
    assert any(event.payload.get("text") == "done: handoff package" for event in events)
    unified = store.load_unified("deliver-after-ack")
    assert [segment.runtime_id for segment in unified.segments] == ["native", "fake-target"]
    assert unified.handoff["target_state"] == "delivered"
    await target.close()


async def test_delivery_failure_is_a_target_failure_not_a_rollback(tmp_path):
    """After acknowledgement the target may have mutated: a failed delivery
    closes it and records the failure, but never resumes the source."""
    from garuda.core.sessions import SessionStore
    from garuda.runtime.handoff import HandoffDeliveryError

    store = SessionStore(tmp_path / "sessions")
    source = await _native_source(tmp_path, store, "deliver-fails")
    target = FakeRuntime(FakeScenario.SUCCESS, runtime_id="fake-target")

    async def fail_delivery(_runtime):
        raise RuntimeError("package rejected")

    with pytest.raises(HandoffDeliveryError, match="package rejected") as caught:
        await execute_handoff(
            session_id="deliver-fails",
            source=source,
            target_factory=lambda: target,
            store=store,
            deliver=fail_delivery,
        )
    assert caught.value.transaction.phase is HandoffPhase.ACKNOWLEDGED
    assert source.state is LifecycleState.CLOSED
    assert target.state is LifecycleState.CLOSED
    unified = store.load_unified("deliver-fails")
    assert unified.active.runtime_id == "fake-target"
    assert unified.handoff["state"] == "acknowledged"
    assert unified.handoff["target_state"] == "failed"
    assert unified.handoff["reason"].startswith("target_delivery")


async def test_target_bind_failure_returns_ownership_to_the_source(tmp_path):
    """A target that cannot record its child is closed before any turn, and
    the session records the source as its owner again."""
    from garuda.core.sessions import SessionStore

    store = SessionStore(tmp_path / "sessions")
    source = await _native_source(tmp_path, store, "bind-fails")
    target = FakeRuntime(FakeScenario.SUCCESS, runtime_id="fake-target")

    def _bind(_store):
        raise OSError("cannot record child")

    target.bind_session = _bind
    with pytest.raises(HandoffError, match="could not be bound"):
        await execute_handoff(
            session_id="bind-fails",
            source=source,
            target_factory=lambda: target,
            store=store,
        )
    assert source.state is LifecycleState.IDLE
    assert target.state is LifecycleState.CLOSED
    unified = store.load_unified("bind-fails")
    assert [segment.runtime_id for segment in unified.segments] == [
        "native",
        "fake-target",
        "native",
    ]
    assert unified.handoff["state"] == "failed"
    assert unified.handoff["reason"] == "target_binding"


async def test_illegal_moves_and_double_begin_fail():
    tx = HandoffTransaction(session_id="s")
    with pytest.raises(HandoffError, match="nothing to cancel"):
        await tx.cancel(FakeRuntime())
    source = await _started()()
    with pytest.raises(HandoffError, match="cannot start target"):
        await tx.start_target(source, FakeRuntime())
    await _begin(tx, source)
    with pytest.raises(HandoffError, match="already"):
        await _begin(tx, source)
    with pytest.raises(HandoffError, match="cannot acknowledge"):
        await tx.acknowledge(source, FakeRuntime())


async def test_source_failure_mid_begin_reaches_failed():
    source = await _started()()
    tx = HandoffTransaction(session_id="s")
    with pytest.raises(HandoffError, match="source side failed"):
        await _begin(tx, source, checkpoint=_boom)
    assert tx.phase is HandoffPhase.FAILED
    assert source.state is LifecycleState.IDLE


def _boom():
    raise RuntimeError("checkpoint disk gone")


async def _native_source(tmp_path, store, session_id: str):
    """A real `NativeGarudaRuntime` source with a working prompt driver."""
    from garuda.core.events import EventStore
    from garuda.core.loop import DefaultAgent
    from garuda.core.permissions import PermissionEngine
    from garuda.core.sessions import SessionStore
    from garuda.model.protocol import ModelResponse
    from garuda.model.script_model import ScriptModel
    from garuda.runtime.native import NativeGarudaRuntime
    from garuda.tools import tools_for_names
    from garuda.types import AgentConfig, ToolCall
    from garuda.workspace.local import LocalEnvironment

    assert isinstance(store, SessionStore)
    model = ScriptModel(
        responses=[
            ModelResponse(
                content=None,
                tool_calls=[
                    ToolCall(id="1", name="task_complete", arguments={"summary": "ok"})
                ],
            )
        ]
    )
    agent = DefaultAgent()
    tools = tools_for_names(["task_complete"])
    config = AgentConfig(max_turns=5, enable_verifier=False, permission_mode="yolo")
    permissions = PermissionEngine(mode="yolo")

    async def _driver(*, task: str, turn: int, trail: EventStore):
        env = LocalEnvironment(workspace_root=tmp_path)
        return await agent.run(
            task=task,
            model=model,
            env=env,
            tools=tools,
            config=config,
            events=trail,
            permissions=permissions,
        )

    runtime = NativeGarudaRuntime(
        agent=agent,
        model=model,
        tools=tools,
        config=config,
        permissions=permissions,
        store=store,
    )
    runtime.install_driver(_driver)
    await runtime.start(task="source work", session_id=session_id)
    return runtime


async def test_execute_handoff_success_uses_production_runtime_and_store(tmp_path):
    """Success end-to-end: real source runtime → real target, store + pack."""
    from garuda.context.pack import ContextPackManager, sync_context_pack
    from garuda.context.schemas import parse_current_task, parse_handoff
    from garuda.context.state_card import WorkingState
    from garuda.core.sessions import SessionStore
    from garuda.runtime.fake import FakeRuntime, FakeScenario
    from garuda.runtime.handoff import HandoffPhase, execute_handoff
    from garuda.runtime.protocol import LifecycleState

    store = SessionStore(tmp_path / "sessions")
    source = await _native_source(tmp_path, store, "handoff-e2e-ok")
    pack_root = tmp_path / ".context"
    manager = ContextPackManager(pack_root)
    working = WorkingState(task="source work")
    working.note_check("pytest -q", 0, turn=1)

    def _checkpoint():
        store.checkpoint_state("handoff-e2e-ok", working.to_dict())
        sync_context_pack(
            manager, working, source_runtime="native", session_id="handoff-e2e-ok"
        )

    seen: list = []
    tx, target = await execute_handoff(
        session_id="handoff-e2e-ok",
        source=source,
        target_factory=lambda: FakeRuntime(FakeScenario.SUCCESS, runtime_id="target-ok"),
        store=store,
        checkpoint=_checkpoint,
        capture=lambda: {"diff": "M a.py"},
        generate=lambda: sync_context_pack(
            manager, working, source_runtime="native", session_id="handoff-e2e-ok"
        ),
        emit=seen.append,
    )
    assert tx.phase is HandoffPhase.ACKNOWLEDGED
    assert source.state is LifecycleState.CLOSED
    assert target.state is LifecycleState.IDLE
    recorded = store.load_unified("handoff-e2e-ok").handoff
    assert recorded["state"] == "acknowledged"
    assert recorded["target_runtime"] == "target-ok"
    # Pack was generated through the single writer during the transaction.
    current_doc, _ = parse_current_task(
        manager.current_task_path.read_text(encoding="utf-8")
    )
    assert current_doc.task == "source work"
    assert current_doc.evidence == ("pytest -q (exit 0)",)
    handoff_doc, _ = parse_handoff(manager.handoff_path.read_text(encoding="utf-8"))
    assert handoff_doc.garuda_session_id == "handoff-e2e-ok"
    assert [e.payload["handoff_phase"] for e in seen][:3] == [
        "pausing",
        "checkpointing",
        "capturing",
    ]


async def test_execute_handoff_target_failure_keeps_source_authoritative(tmp_path):
    """Target-start failure end-to-end: source stays resumable, store failed."""
    from garuda.context.pack import ContextPackManager, sync_context_pack
    from garuda.context.state_card import WorkingState
    from garuda.core.sessions import SessionStore
    from garuda.runtime.fake import FakeRuntime, FakeScenario
    from garuda.runtime.handoff import HandoffError, execute_handoff
    from garuda.runtime.protocol import LifecycleState

    store = SessionStore(tmp_path / "sessions")
    source = await _native_source(tmp_path, store, "handoff-e2e-fail")
    manager = ContextPackManager(tmp_path / ".context")
    working = WorkingState(task="source work")
    working.note_check("pytest -q", 0, turn=1)

    with __import__("pytest").raises(HandoffError, match="target startup failed"):
        await execute_handoff(
            session_id="handoff-e2e-fail",
            source=source,
            target_factory=lambda: FakeRuntime(
                FakeScenario.STARTUP_FAILURE, runtime_id="target-bad"
            ),
            store=store,
            checkpoint=lambda: sync_context_pack(
                manager, working, source_runtime="native", session_id="handoff-e2e-fail"
            ),
            capture=lambda: {"diff": "M a.py"},
        )
    # Rolled back: exactly one resumable owner — the source.
    assert source.state is LifecycleState.IDLE
    recorded = store.load_unified("handoff-e2e-fail").handoff
    assert recorded["state"] == "failed"
    turn = await source.prompt("still authoritative")
    assert turn >= 1
    assert source.state is LifecycleState.IDLE
    await source.close()


async def test_execute_handoff_target_factory_failure_resumes_source(tmp_path):
    from garuda.core.sessions import SessionStore
    from garuda.runtime.handoff import HandoffError, execute_handoff
    from garuda.runtime.protocol import LifecycleState

    store = SessionStore(tmp_path / "sessions")
    source = await _native_source(tmp_path, store, "handoff-factory-fail")

    def _bad_factory():
        raise RuntimeError("factory exploded")

    with pytest.raises(HandoffError, match="target construction failed"):
        await execute_handoff(
            session_id="handoff-factory-fail",
            source=source,
            target_factory=_bad_factory,
            store=store,
        )
    assert source.state is LifecycleState.IDLE
    assert store.load_unified("handoff-factory-fail").handoff["state"] == "failed"


async def test_acknowledgement_audit_failure_never_transfers_ownership(tmp_path):
    from garuda.core.sessions import SessionStore
    from garuda.runtime.fake import FakeRuntime, FakeScenario
    from garuda.runtime.handoff import HandoffError, execute_handoff
    from garuda.runtime.protocol import LifecycleState

    store = SessionStore(tmp_path / "sessions")
    source = await _native_source(tmp_path, store, "handoff-audit-fail")
    target = FakeRuntime(FakeScenario.SUCCESS, runtime_id="target-audit-fail")
    original_record = store.record_handoff

    def _record(session_id, *, state, attempts=0, **extra):
        if state == "acknowledged":
            raise OSError("disk full")
        return original_record(session_id, state=state, attempts=attempts, **extra)

    store.record_handoff = _record
    with pytest.raises(HandoffError, match="acknowledge audit failed"):
        await execute_handoff(
            session_id="handoff-audit-fail",
            source=source,
            target_factory=lambda: target,
            store=store,
        )
    assert source.state is LifecycleState.IDLE
    assert target.state is LifecycleState.CLOSED


async def test_double_fault_on_return_to_source_names_the_manual_fix(tmp_path, monkeypatch):
    """Acknowledgement fails and the return-to-source write fails too: the
    caller gets an error naming `garuda runtime reclaim`, not a log line."""
    from garuda.core.sessions import SessionStore

    store = SessionStore(tmp_path / "sessions")
    source = await _native_source(tmp_path, store, "double-fault")
    target = FakeRuntime(FakeScenario.SUCCESS, runtime_id="fake-target")

    async def close_fails():
        raise OSError("source will not close")

    monkeypatch.setattr(source, "close", close_fails)
    real_record = store.record_handoff

    def record(session_id, *, state, **kwargs):
        if state == "failed":
            raise OSError("disk full")
        return real_record(session_id, state=state, **kwargs)

    monkeypatch.setattr(store, "record_handoff", record)
    with pytest.raises(HandoffError, match="garuda runtime reclaim --session double-fault"):
        await execute_handoff(
            session_id="double-fault",
            source=source,
            target_factory=lambda: target,
            store=store,
        )


def test_reclaim_recovers_the_record_a_double_fault_leaves(tmp_path):
    """The state the double-fault error points at: an ACP owner with no
    recorded target state but a retired child. Reclaim (through the real
    recovery pass) returns it to native, as the error message promises."""
    from garuda.acp.authority import AuthorityMap
    from garuda.core.sessions import SessionStore
    from garuda.runtime.recovery import RestartState, classify, reclaim_native
    from garuda.runtime.session import RuntimeSegment

    store = SessionStore(tmp_path / "sessions")
    store.begin("df", task="t", model="m", agent="a", workspace=str(tmp_path))
    store.checkpoint_messages("df", [])
    store.ensure_unified("df")
    owners = {family: "agent" for family in ("edit", "terminal", "mcp", "approval")}
    store.record_handoff(
        "df",
        state="acknowledged",
        attempts=1,
        active_segment=RuntimeSegment(
            runtime_id="ext",
            kind="acp",
            native_session_id="agent-1",
            capabilities=AuthorityMap(owners=owners).to_snapshot(),
        ),
    )
    store.update_meta(
        "df",
        {"runtime_children": [{"runtime_id": "ext", "session_id": "df", "state": "exited"}]},
    )
    assert classify(store, "df").state is RestartState.EXTERNAL
    report = reclaim_native(store, "df")
    assert report.state is RestartState.RESUMABLE
    assert store.load_unified("df").handoff["reclaimed_from"] == "ext"
