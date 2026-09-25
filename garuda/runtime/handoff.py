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

import logging
from collections.abc import Callable
from enum import Enum
from pathlib import Path
from typing import Any

from garuda.runtime.events import RuntimeEvent, RuntimeEventKind
from garuda.runtime.protocol import (
    AgentRuntimeError,
    LifecycleState,
)

logger = logging.getLogger(__name__)


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
        store=None,
    ):
        self._session_id = session_id
        self._phase = HandoffPhase.IDLE
        self._emit_sink = emit
        self._store = store
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
            try:
                await self._resume_source(source)
            except Exception as resume_exc:
                raise self._fail(
                    f"source side failed: {exc}; source resume failed: {resume_exc}"
                ) from exc
            raise self._fail(f"source side failed: {exc}") from exc

    async def start_target(self, source, target) -> Any:
        """Start the target runtime. Failure rolls back to a resumable source."""
        if self._phase is not HandoffPhase.GENERATING:
            raise HandoffError(f"cannot start target from {self._phase.value}")
        self._move(HandoffPhase.STARTING_TARGET, target=target.runtime_id)
        try:
            self.target_info = await target.start(task=f"handoff from {self._session_id}")
        except Exception as exc:
            try:
                await target.close()
            except Exception as close_exc:
                raise self._fail(
                    "target startup failed and target cleanup could not be confirmed; "
                    f"source remains paused: {close_exc}"
                ) from exc
            await self._rollback(source, f"target startup failed: {exc}")
            raise HandoffError(f"target startup failed: {exc}") from exc
        self._move(HandoffPhase.AWAITING_ACK, target=target.runtime_id)
        return self.target_info

    async def acknowledge(self, source, target) -> None:
        """Transfer the single mutating ownership to the target."""
        if self._phase is not HandoffPhase.AWAITING_ACK:
            raise HandoffError(f"cannot acknowledge from {self._phase.value}")
        if source.state is not LifecycleState.PAUSED_AT_BOUNDARY:
            if target.state not in (LifecycleState.CLOSED, LifecycleState.FAILED):
                try:
                    await target.close()
                except Exception as exc:
                    raise self._fail(
                        "source is active and target cleanup could not be confirmed; "
                        f"manual recovery required: {exc}"
                    ) from exc
            raise HandoffError(
                "source is not frozen at the switch boundary; "
                "two active mutating owners cannot exist"
            )
        if target.state not in (LifecycleState.IDLE, LifecycleState.RUNNING):
            raise HandoffError("target is not active; acknowledgement refused")
        try:
            await source.close()
        except Exception as exc:
            try:
                await target.close()
                await self._resume_source(source)
            except Exception as cleanup_exc:
                raise self._fail(
                    "source close failed and rollback could not be confirmed; "
                    f"manual recovery required: {cleanup_exc}"
                ) from exc
            self._move(HandoffPhase.ROLLED_BACK, reason=f"source close failed: {exc}")
            raise HandoffError(f"source close failed: {exc}") from exc
        self._move(HandoffPhase.ACKNOWLEDGED, target=target.runtime_id)

    async def cancel(self, source, target=None, *, reason: str = "") -> None:
        """Cancel the switch. The source returns to a resumable state and a
        started target is closed, so exactly one resumable owner remains."""
        if self._phase is HandoffPhase.IDLE:
            raise HandoffError("nothing to cancel")
        if self._store is not None:
            try:
                from garuda.runtime.recovery import record_cancel

                record_cancel(
                    self._store,
                    self._session_id,
                    boundary="switch",
                    reason=reason or "cancelled",
                )
            except Exception as exc:
                raise self._fail(f"handoff cancellation audit failed: {exc}") from exc
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
            try:
                await target.close()
            except Exception as exc:
                raise self._fail(
                    "target cleanup could not be confirmed; source remains paused: "
                    f"{exc}"
                ) from exc
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


async def execute_handoff(
    *,
    session_id: str,
    source,
    target_factory: Callable[[], Any],
    store=None,
    checkpoint: Callable[[], None] | None = None,
    capture: Callable[[], dict[str, Any]] | None = None,
    generate: Callable[[], None] | None = None,
    emit: Callable[[RuntimeEvent], None] | None = None,
    workspace: str | Path | None = None,
) -> tuple[HandoffTransaction, Any]:
    """Run the full pause → checkpoint → capture → start → ack transaction.

    The production entry point `HandoffTransaction` unit tests never reached:
    every `begin`/`start_target`/`acknowledge` here runs against real
    `AgentRuntime` instances, with the session store recording the outcome so
    exactly one authoritative owner survives either branch.

    - Success returns `(tx, target)` with `tx.phase == ACKNOWLEDGED`,
      `source` CLOSED and `target` active; the store records `acknowledged`.
    - Target-startup failure rolls back inside `start_target` (source resumed
      to IDLE and still promptable) and the store records `failed`; the
      `HandoffError` propagates so callers cannot mistake it for a move.
    - A `store.record_handoff` failure fails closed: the transaction moves to
      FAILED rather than transferring ownership without an audit trail.
    - With `workspace` (and a store holding the session's recorded baseline),
      the capture carries the authoritative delta — changed vs preexisting
      files from the exact start-of-session baseline, or only an explicit
      `workspace_attribution` reason when the baseline is unattributable —
      and the acknowledge record persists the baseline commit for the target
      session. No production entry point passes `workspace` yet; until one
      does, product handoffs carry no workspace delta.
    """
    tx = HandoffTransaction(session_id=session_id, emit=emit, store=store)
    if workspace is not None:
        if store is None:
            raise HandoffError(
                "handoff with a workspace requires a session store holding its baseline"
            )
        try:
            from garuda.workspace import evidence

            # Preflight before pausing anything: a handoff cannot claim a
            # workspace delta it cannot derive from the recorded
            # start-of-session baseline, so refuse while the source is live.
            evidence.load_session_delta(store, session_id, workspace)
        except Exception as exc:
            raise HandoffError(
                f"handoff refused: authoritative workspace delta is unavailable: {exc}"
            ) from exc
    if store is not None:
        try:
            store.record_handoff(session_id, state="prepared", attempts=1)
        except Exception as exc:
            logger.warning("Handoff prepare audit failed", exc_info=True)
            raise HandoffError(f"handoff prepare audit failed: {exc}") from exc

    def _capture_with_delta() -> dict[str, Any]:
        data = dict(capture() if capture else {})
        if workspace is not None:
            from garuda.workspace import evidence

            # Recomputed after the pause so the package reflects the paused
            # tree, not the preflight snapshot. A failure here raises inside
            # `begin`, which resumes the source instead of transferring.
            delta = evidence.load_session_delta(store, session_id, workspace)
            data.setdefault("workspace_attribution", delta.attribution)
            if delta.attributable:
                data.setdefault("baseline_commit", delta.baseline_commit)
                data.setdefault("changed", list(delta.changed))
                data.setdefault("preexisting", list(delta.preexisting))
        return data

    try:
        await tx.begin(
            source,
            checkpoint=checkpoint or (lambda: None),
            capture=_capture_with_delta,
            generate=generate,
        )
    except HandoffError:
        # The source side failed (and was resumed, or the error says it could
        # not be). Record it so the prepared audit is not left dangling.
        if store is not None:
            try:
                store.record_handoff(
                    session_id, state="failed", attempts=1, reason="source_side"
                )
            except Exception:
                logger.warning("Handoff failure audit failed", exc_info=True)
        raise
    try:
        target = target_factory()
    except Exception as exc:
        await tx.cancel(source, reason="target construction failed")
        if store is not None:
            try:
                store.record_handoff(
                    session_id, state="failed", attempts=1, reason="target_factory"
                )
            except Exception:
                logger.warning("Handoff failure audit failed", exc_info=True)
        raise HandoffError(f"target construction failed: {exc}") from exc
    try:
        await tx.start_target(source, target)
    except HandoffError:
        if store is not None:
            try:
                store.record_handoff(
                    session_id, state="failed", attempts=1, reason="target_startup"
                )
            except Exception:
                logger.warning("Handoff failure audit failed", exc_info=True)
        raise
    # Persist the ownership decision before closing the source. If this write
    # fails, the target is closed and the source resumes; ownership never moves
    # without a durable recovery record. A crash after this write still has one
    # potential mutator because the source is frozen at its boundary.
    if store is not None:
        try:
            extra: dict[str, Any] = {
                "target_runtime": getattr(target, "runtime_id", ""),
            }
            if "baseline_commit" in tx.captured:
                extra["baseline_commit"] = tx.captured["baseline_commit"]
            store.record_handoff(
                session_id,
                state="acknowledged",
                attempts=1,
                **extra,
            )
        except Exception as exc:
            logger.warning("Handoff acknowledge audit failed", exc_info=True)
            await tx.cancel(source, target, reason="acknowledgement audit failed")
            raise HandoffError(f"handoff acknowledge audit failed: {exc}") from exc
    try:
        await tx.acknowledge(source, target)
    except HandoffError:
        if store is not None:
            try:
                store.record_handoff(
                    session_id, state="failed", attempts=1, reason="acknowledgement"
                )
            except Exception:
                logger.warning("Handoff rollback audit failed", exc_info=True)
        raise
    return tx, target
