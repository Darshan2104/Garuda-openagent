"""Handoff transaction tests for issue #18 (P0.10).

Every transition, rollback branch, cancellation point, and restart state:
success transfers the single ownership, target failure keeps the source
resumable, cancellation lands recoverable, and each move emits a typed event.
"""

import pytest

from garuda.runtime.events import RuntimeEventKind
from garuda.runtime.fake import FakeRuntime, FakeScenario
from garuda.runtime.handoff import HandoffError, HandoffPhase, HandoffTransaction
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
