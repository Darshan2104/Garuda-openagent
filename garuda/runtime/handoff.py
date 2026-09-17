"""Handoff transaction state machine (P0.10, issue #18).

A runtime switch is a transaction, never a jump: pause the source at a safe
boundary, checkpoint it, capture diff/evidence, generate the handoff package,
start the target, and transfer the single mutating ownership only on
acknowledgement. Any failure or cancellation before acknowledgement rolls back
to a resumable source — the target never inherits a half-moved session.

Steps that touch the outside world (diff capture, pack writing, target
startup) are injected callables, so the machine itself is deterministic and
fully fault-injectable.
"""

from __future__ import annotations

from collections.abc import Callable
from enum import Enum
from typing import Any

from garuda.runtime.events import RuntimeEvent, RuntimeEventKind
from garuda.runtime.protocol import (
    AgentRuntimeError,
    LifecycleState,
)


class HandoffPhase(str, Enum):
    IDLE = "idle"
    PAUSING = "pausing"
    CHECKPOINTING = "checkpointing"
    CAPTURING = "capturing"
    GENERATING = "generating"
    STARTING_TARGET = "starting_target"
    AWAITING_ACK = "awaiting_ack"
    ACKNOWLEDGED = "acknowledged"
    ROLLED_BACK = "rolled_back"
    FAILED = "failed"
    CANCELLED = "cancelled"


_ALLOWED: dict[HandoffPhase, frozenset[HandoffPhase]] = {
    HandoffPhase.IDLE: frozenset({HandoffPhase.PAUSING}),
    HandoffPhase.PAUSING: frozenset({HandoffPhase.CHECKPOINTING, HandoffPhase.CANCELLED, HandoffPhase.FAILED}),
    HandoffPhase.CHECKPOINTING: frozenset({HandoffPhase.CAPTURING, HandoffPhase.CANCELLED, HandoffPhase.FAILED}),
    HandoffPhase.CAPTURING: frozenset({HandoffPhase.GENERATING, HandoffPhase.CANCELLED, HandoffPhase.FAILED}),
    HandoffPhase.GENERATING: frozenset({HandoffPhase.STARTING_TARGET, HandoffPhase.CANCELLED, HandoffPhase.FAILED}),
    HandoffPhase.STARTING_TARGET: frozenset(
        {HandoffPhase.AWAITING_ACK, HandoffPhase.ROLLED_BACK, HandoffPhase.CANCELLED, HandoffPhase.FAILED}
    ),
    HandoffPhase.AWAITING_ACK: frozenset(
        {HandoffPhase.ACKNOWLEDGED, HandoffPhase.ROLLED_BACK, HandoffPhase.CANCELLED, HandoffPhase.FAILED}
    ),
    HandoffPhase.ACKNOWLEDGED: frozenset(),
    HandoffPhase.ROLLED_BACK: frozenset(),
    HandoffPhase.FAILED: frozenset(),
    HandoffPhase.CANCELLED: frozenset(),
}


class HandoffError(AgentRuntimeError):
    """An illegal transaction move or a violated ownership invariant."""


class HandoffTransaction:
    """One boundary-only runtime switch."""

    def __init__(
        self,
        *,
        session_id: str,
        emit: Callable[[RuntimeEvent], None] | None = None,
    ):
        self._session_id = session_id
        self._phase = HandoffPhase.IDLE
        self._emit_sink = emit
        self._seq = 0
        self.captured: dict[str, Any] = {}
        self.target_info: Any = None

    @property
    def phase(self) -> HandoffPhase:
        return self._phase

    def _move(self, to: HandoffPhase, **payload: Any) -> None:
        if to not in _ALLOWED[self._phase]:
            raise HandoffError(
                f"invalid handoff transition: {self._phase.value} -> {to.value}"
            )
        self._phase = to
        if self._emit_sink is not None:
            self._seq += 1
            self._emit_sink(
                RuntimeEvent(
                    kind=RuntimeEventKind.LIFECYCLE,
                    session_id=self._session_id,
                    turn=0,
                    seq=self._seq,
                    payload={"handoff_phase": to.value, **payload},
                )
            )

    def _fail(self, reason: str) -> HandoffError:
        self._move(HandoffPhase.FAILED, reason=reason)
        return HandoffError(reason)

    async def begin(
        self,
        source,
        *,
        checkpoint: Callable[[], None],
        capture: Callable[[], dict[str, Any]] | None = None,
        generate: Callable[[], None] | None = None,
    ) -> None:
        """Run the source side: pause, checkpoint, capture, generate."""
        if self._phase is not HandoffPhase.IDLE:
            raise HandoffError(f"transaction already {self._phase.value}")
        if source.state is LifecycleState.RUNNING:
            raise HandoffError("source is mid-turn; switch at a boundary only")
        try:
            self._move(HandoffPhase.PAUSING, source=source.runtime_id)
            await source.pause_at_boundary()
            self._move(HandoffPhase.CHECKPOINTING)
            checkpoint()
            self._move(HandoffPhase.CAPTURING)
            self.captured = dict(capture() if capture else {})
            self._move(HandoffPhase.GENERATING)
            if generate:
                generate()
        except HandoffError:
            raise
        except Exception as exc:
            raise self._fail(f"source side failed: {exc}") from exc

    async def start_target(self, source, target) -> Any:
        """Start the target runtime. Failure rolls back to a resumable source."""
        if self._phase is not HandoffPhase.GENERATING:
            raise HandoffError(f"cannot start target from {self._phase.value}")
        self._move(HandoffPhase.STARTING_TARGET, target=target.runtime_id)
        try:
            self.target_info = await target.start(task=f"handoff from {self._session_id}")
        except Exception as exc:
            await self._rollback(source, f"target startup failed: {exc}")
            raise HandoffError(f"target startup failed: {exc}") from exc
        self._move(HandoffPhase.AWAITING_ACK, target=target.runtime_id)
        return self.target_info

    async def acknowledge(self, source, target) -> None:
        """Transfer the single mutating ownership to the target."""
        if self._phase is not HandoffPhase.AWAITING_ACK:
            raise HandoffError(f"cannot acknowledge from {self._phase.value}")
        if source.state is not LifecycleState.PAUSED_AT_BOUNDARY:
            raise HandoffError(
                "source is not frozen at the switch boundary; "
                "two active mutating owners cannot exist"
            )
        if target.state not in (LifecycleState.IDLE, LifecycleState.RUNNING):
            raise HandoffError("target is not active; acknowledgement refused")
        await source.close()
        self._move(HandoffPhase.ACKNOWLEDGED, target=target.runtime_id)

    async def cancel(self, source, target=None, *, reason: str = "") -> None:
        """Cancel the switch. The source returns to a resumable state and a
        started target is closed, so exactly one resumable owner remains."""
        if self._phase is HandoffPhase.IDLE:
            raise HandoffError("nothing to cancel")
        if self._phase in (
            HandoffPhase.ACKNOWLEDGED,
            HandoffPhase.ROLLED_BACK,
            HandoffPhase.FAILED,
            HandoffPhase.CANCELLED,
        ):
            raise HandoffError(f"cannot cancel from terminal {self._phase.value}")
        if target is not None and target.state not in (
            LifecycleState.CLOSED,
            LifecycleState.FAILED,
        ):
            await target.close()
        await self._resume_source(source)
        self._move(HandoffPhase.CANCELLED, reason=reason)

    async def _rollback(self, source, reason: str) -> None:
        await self._resume_source(source)
        self._move(HandoffPhase.ROLLED_BACK, reason=reason)

    async def _resume_source(self, source) -> None:
        if source.state is LifecycleState.PAUSED_AT_BOUNDARY:
            resume = getattr(source, "resume_from_pause", None)
            if resume is not None:
                await resume()
