"""Shared lifecycle conformance suite (P0.4 #11, run by every adapter since).

`run_conformance_suite` is the contract every runtime — native, fake, or ACP —
must satisfy: session/turn correlation and a stable discriminant on every
event, unambiguous terminal state, and rejection (not silent storage) of
anything past it. It lives in the package (not the tests) so the contract
matrix and future adapters import one definition.
"""

from __future__ import annotations

from garuda.runtime.events import RuntimeEventKind
from garuda.runtime.protocol import (
    AgentRuntime,
    LifecycleState,
    RuntimeClosedError,
)


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
    try:
        await rt.prompt("too late")
    except RuntimeClosedError:
        return
    raise AssertionError("prompt after close must raise RuntimeClosedError")
