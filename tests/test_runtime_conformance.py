"""Shared lifecycle conformance suite for issue #11 (P0.4).

`run_conformance_suite` is the contract every runtime — native, fake, or a
future ACP adapter — must satisfy. It asserts the acceptance criteria directly:
session/turn correlation and a stable discriminant on every event, unambiguous
terminal state, and rejection (not silent storage) of anything past it.
"""

import pytest

from garuda.runtime import (
    AgentRuntime,
    LifecycleState,
    RuntimeClosedError,
    RuntimeEventKind,
    RuntimeProtocolError,
)
from garuda.runtime.fake import FakeRuntime, FakeScenario


async def run_conformance_suite(make_runtime) -> None:
    """Full lifecycle against one runtime instance. Raises on any violation."""
    rt = make_runtime()
    assert isinstance(rt, AgentRuntime)

    info = await rt.start(task="conformance probe", session_id="conf")
    assert info.native_session_id
    assert info.version

    turn = await rt.prompt("do the thing")
    assert turn == 1

    events, cursor = await rt.poll_events(0)
    assert events, "a prompt must produce observable events"
    assert cursor == len(events)
    seqs = [e.seq for e in events]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs), "seqs strictly increase"
    for event in events:
        assert event.session_id, "every event carries its session"
        assert event.turn in (0, 1), "every event correlates to its turn"
        assert isinstance(event.kind, RuntimeEventKind), "stable discriminant"
    assert any(e.turn == 1 for e in events), "the prompt produced observable events"
    assert any(e.kind is RuntimeEventKind.LIFECYCLE for e in events)

    # Nothing observable is lost: polling from the returned cursor is empty,
    # polling from zero replays the same prefix.
    rest, _ = await rt.poll_events(cursor)
    assert rest == []
    replay, _ = await rt.poll_events(0)
    assert [e.seq for e in replay] == seqs

    await rt.close()
    assert rt.state is LifecycleState.CLOSED
    with pytest.raises(RuntimeClosedError):
        await rt.prompt("too late")


async def test_success_conformance():
    await run_conformance_suite(lambda: FakeRuntime(FakeScenario.SUCCESS))


async def test_streaming_conformance():
    rt = FakeRuntime(FakeScenario.STREAMING)
    await run_conformance_suite(lambda: rt)
    events, _ = await rt.poll_events(0)
    chunks = [e for e in events if e.kind is RuntimeEventKind.MESSAGE]
    assert len(chunks) == 3
    assert "".join(c.payload["chunk"] for c in chunks) == "hello, world"


async def test_approval_allow_and_deny():
    rt = FakeRuntime(FakeScenario.APPROVAL)
    await rt.start(task="needs approval", session_id="a1")
    await rt.prompt("delete everything")
    events, _ = await rt.poll_events(0)
    request = next(e for e in events if e.kind is RuntimeEventKind.APPROVAL_REQUEST)
    assert request.payload["approval_id"] == "apr-1"

    # Answering a different approval is a protocol violation, not a silent no-op.
    with pytest.raises(RuntimeProtocolError):
        await rt.permission_response(approval_id="apr-zzz", allow=True)

    await rt.permission_response(approval_id="apr-1", allow=True)
    assert rt.state is LifecycleState.IDLE
    events, _ = await rt.poll_events(0)
    assert any(e.kind is RuntimeEventKind.TOOL_RESULT for e in events)

    await rt.prompt("do it again")
    await rt.permission_response(approval_id="apr-1", allow=False)
    events, _ = await rt.poll_events(0)
    assert any(e.kind is RuntimeEventKind.ERROR for e in events)
    await rt.close()


async def test_malformed_stream_is_typed():
    rt = FakeRuntime(FakeScenario.MALFORMED)
    await rt.start(task="x", session_id="m1")
    await rt.prompt("x")
    with pytest.raises(RuntimeProtocolError):
        await rt.poll_events(0)


async def test_delayed_boundary_pause():
    rt = FakeRuntime(FakeScenario.DELAYED_BOUNDARY)
    await rt.start(task="x", session_id="d1")
    await rt.pause_at_boundary()
    assert rt.state is LifecycleState.PAUSED_AT_BOUNDARY
    await rt.resume_from_pause()
    assert rt.state is LifecycleState.IDLE
    await rt.close()


async def test_cancel_is_terminal_and_unambiguous():
    rt = FakeRuntime(FakeScenario.CANCELLATION)
    await rt.start(task="x", session_id="c1")
    await rt.prompt("slow work")
    await rt.cancel(reason="user asked")
    assert rt.state is LifecycleState.CLOSED
    events, _ = await rt.poll_events(0)
    terminal = [e for e in events if e.is_terminal()]
    assert len(terminal) == 1
    assert terminal[0].payload["state"] == "cancelled"
    # Cancelling twice never resurrects or duplicates the terminal event.
    await rt.cancel(reason="again")
    events, _ = await rt.poll_events(0)
    assert sum(1 for e in events if e.is_terminal()) == 1


async def test_late_emission_is_rejected_not_stored():
    rt = FakeRuntime(FakeScenario.MUTATION_ATTEMPT)
    await rt.start(task="x", session_id="mu1")
    await rt.prompt("write outside the workspace")
    await rt.close()
    with pytest.raises(RuntimeClosedError):
        rt.attempt_late_emit()
    with pytest.raises(RuntimeClosedError):
        await rt.prompt("x")
