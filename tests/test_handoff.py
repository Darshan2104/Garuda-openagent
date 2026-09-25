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


async def test_delivery_runs_on_the_target_before_acknowledgement():
    source = await _started()()
    target = FakeRuntime(FakeScenario.SUCCESS, runtime_id="fake-target")
    delivered: list[str] = []

    async def deliver(runtime):
        delivered.append("handoff package")
        await runtime.prompt(delivered[-1])

    tx, returned = await execute_handoff(
        session_id="s",
        source=source,
        target_factory=lambda: target,
        deliver=deliver,
    )
    assert tx.phase is HandoffPhase.ACKNOWLEDGED
    assert returned is target
    assert source.state is LifecycleState.CLOSED
    assert target.state is LifecycleState.IDLE
    events, _ = await target.poll_events(0)
    assert any(event.payload.get("text") == "done: handoff package" for event in events)
    await target.close()
    assert target.state is LifecycleState.CLOSED


async def test_delivery_failure_reaps_target_and_rolls_back_source():
    source = await _started()()
    target = FakeRuntime(FakeScenario.SUCCESS, runtime_id="fake-target")

    async def fail_delivery(_runtime):
        raise RuntimeError("package rejected")

    with pytest.raises(HandoffError, match="delivery failed"):
        await execute_handoff(
            session_id="s",
            source=source,
            target_factory=lambda: target,
            deliver=fail_delivery,
        )
    assert source.state is LifecycleState.IDLE
    assert target.state is LifecycleState.CLOSED


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
