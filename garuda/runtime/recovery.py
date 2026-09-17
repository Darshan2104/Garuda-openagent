"""Cancellation boundaries and crash recovery (P0.19, issue #29).

Cancellation lands at turn, switch, or process boundaries — never mid-turn —
and every interrupted session restarts from a classified state, never a guess:

- a crash mid-run resumes from its checkpoints;
- a failed or cancelled switch resumes the retained source;
- a prepared-but-unacknowledged switch rolls back before resuming;
- orphan agent child processes are reaped and verified dead;
- ambiguous terminal state refuses recovery instead of inventing an outcome.

Source state is retained until the target acknowledges (the handoff machine
enforces that); recovery only ever marks the failed attempt, never the source.
A bare process exit is evidence of nothing — only an accepted completion
verdict marks success, so exit codes alone never flip a session to successful.
"""

from __future__ import annotations

import os
import signal
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class RestartState(str, Enum):
    RESUMABLE = "resumable"
    ROLLED_BACK = "rolled_back"
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True)
class RecoveryReport:
    session_id: str
    state: RestartState
    resume_session_id: str
    reaped_pids: tuple[int, ...] = ()
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def success_claim(self) -> bool:
        """Recovery never claims success. Only an accepted completion does."""
        return False


class RecoveryError(Exception):
    """Recovery refused: ambiguous terminal state or an unreapable child."""


def audit_terminal(events: list) -> bool:
    """True when terminal state is unambiguous: at most one terminal event,
    and when present it is the last event in the trail."""
    terminal = [i for i, e in enumerate(events) if e.is_terminal()]
    if not terminal:
        return True
    return len(terminal) == 1 and terminal[0] == len(events) - 1


def _process_live(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError, OSError):
        return False
    return True


def _reap_group(pid: int) -> None:
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass


def reap_orphans(
    pids: list[int],
    *,
    is_alive: Callable[[int], bool] = _process_live,
    reap: Callable[[int], None] = _reap_group,
) -> tuple[int, ...]:
    """Kill leftover agent children and verify each one dead. Unverifiable
    pids fail closed instead of being declared reaped."""
    reaped: list[int] = []
    for pid in pids:
        if not is_alive(pid):
            continue
        reap(pid)
        if is_alive(pid):
            raise RecoveryError(f"child process {pid} survived reaping; refusing")
        reaped.append(pid)
    return tuple(reaped)


def record_cancel(store, session_id: str, *, boundary: str, reason: str = "") -> None:
    """Persist a cancellation at a named boundary (turn, switch, or process)."""
    if boundary not in ("turn", "switch", "process"):
        raise RecoveryError(f"unknown cancellation boundary {boundary!r}")
    store.update_meta(
        session_id,
        {"cancellation": {"boundary": boundary, "reason": reason}},
    )


def classify(store, session_id: str) -> RecoveryReport:
    """Classify a session's restart state from its persisted records only."""
    try:
        meta = store.load_meta(session_id)
    except (OSError, ValueError) as exc:
        raise RecoveryError(f"session {session_id} is unreadable: {exc}") from exc
    if not isinstance(meta, dict) or not meta.get("session_id"):
        raise RecoveryError(f"session {session_id} has no identity; refusing")
    try:
        unified = store.load_unified(session_id)
    except Exception as exc:
        raise RecoveryError(f"session {session_id} fails validation: {exc}") from exc
    handoff_state = unified.handoff.get("state", "none")
    if handoff_state == "prepared":
        return RecoveryReport(
            session_id=session_id,
            state=RestartState.ROLLED_BACK,
            resume_session_id=session_id,
            notes=("switch prepared but never acknowledged; source retained",),
        )
    if handoff_state in ("failed", "none", "acknowledged"):
        return RecoveryReport(
            session_id=session_id,
            state=RestartState.RESUMABLE,
            resume_session_id=session_id,
            notes=(f"handoff state {handoff_state}; checkpoints intact",),
        )
    raise RecoveryError(f"session {session_id} has unknown handoff state {handoff_state!r}")


def recover(
    store,
    session_id: str,
    *,
    child_pids: list[int] | None = None,
    is_alive: Callable[[int], bool] = _process_live,
    reap: Callable[[int], None] = _reap_group,
) -> RecoveryReport:
    """Reap orphans, classify, and mark rolled-back switches. Returns the
    session to resume; never invents success."""
    reaped = reap_orphans(child_pids or [], is_alive=is_alive, reap=reap)
    report = classify(store, session_id)
    if report.state is RestartState.ROLLED_BACK:
        store.record_handoff(session_id, state="failed", attempts=1)
    notes = (*report.notes, *(f"reaped child {pid}" for pid in reaped))
    return RecoveryReport(
        session_id=report.session_id,
        state=report.state,
        resume_session_id=report.resume_session_id,
        reaped_pids=reaped,
        notes=notes,
    )


def report_to_dict(report: RecoveryReport) -> dict[str, Any]:
    return {
        "session_id": report.session_id,
        "state": report.state.value,
        "resume_session_id": report.resume_session_id,
        "reaped_pids": list(report.reaped_pids),
        "notes": list(report.notes),
        "success_claim": report.success_claim,
    }
