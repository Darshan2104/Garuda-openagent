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

import json
import os
import signal
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
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


def _validate_pid(pid: object) -> int:
    """Accept only a safe, concrete child/process-group leader id.

    PID 0 addresses the caller's process group and negative values have
    similarly broad signal semantics. Recovery may only signal an explicitly
    recorded positive child that is not Garuda itself or its current group.
    """
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 1:
        raise RecoveryError(f"invalid persisted child pid {pid!r}")
    if pid in {os.getpid(), os.getpgrp()}:
        raise RecoveryError(f"refusing to signal Garuda's own pid/process group {pid}")
    return pid


def record_child(
    store, session_id: str, *, runtime_id: str, pid: int, process_group: int | None = None
) -> None:
    """Record a Garuda-launched child bound to this persisted session.

    This is the only source `recover()` consults in production.  Binding the
    runtime identity to a known unified segment prevents an arbitrary value in
    a session JSON file from becoming a signal target after restart.
    """
    pid = _validate_pid(pid)
    process_group = _validate_pid(process_group if process_group is not None else pid)
    unified = store.load_unified(session_id)
    if runtime_id not in {segment.runtime_id for segment in unified.segments}:
        raise RecoveryError(
            f"child runtime {runtime_id!r} is not bound to session {session_id}"
        )
    meta = store.load_meta(session_id)
    children = list(meta.get("runtime_children", []))
    children.append(
        {
            "session_id": session_id,
            "runtime_id": runtime_id,
            "pid": pid,
            "process_group": process_group,
            "state": "live",
        }
    )
    store.update_meta(session_id, {"runtime_children": children})


def _recorded_child_pids(store, session_id: str) -> list[int]:
    """Load only well-formed, live identities recorded by Garuda itself."""
    meta = store.load_meta(session_id)
    children = meta.get("runtime_children", [])
    if not isinstance(children, list):
        raise RecoveryError("persisted runtime children must be a list")
    unified = store.load_unified(session_id)
    runtime_ids = {segment.runtime_id for segment in unified.segments}
    pids: list[int] = []
    for child in children:
        if not isinstance(child, dict):
            raise RecoveryError("persisted runtime child must be a mapping")
        if child.get("state", "live") != "live":
            continue
        if child.get("session_id") != session_id:
            raise RecoveryError("persisted runtime child has a mismatched session identity")
        runtime_id = child.get("runtime_id")
        if not isinstance(runtime_id, str) or runtime_id not in runtime_ids:
            raise RecoveryError("persisted runtime child is not bound to a session runtime")
        pid = _validate_pid(child.get("pid"))
        process_group = _validate_pid(child.get("process_group"))
        if process_group != pid:
            raise RecoveryError("persisted child must lead its isolated process group")
        pids.append(pid)
    return pids


def audit_terminal(events: list) -> bool:
    """True when terminal state is unambiguous: at most one terminal event,
    and when present it is the last event in the trail."""
    terminal = [i for i, e in enumerate(events) if e.is_terminal()]
    if not terminal:
        return True
    return len(terminal) == 1 and terminal[0] == len(events) - 1


def _process_live(pid: int) -> bool | None:
    """Probe one pid. True = live, False = dead, None = indeterminate.

    `ProcessLookupError` means dead. `PermissionError` (or any other
    `OSError`) means the process may be alive under another owner — treating
    that as dead would reap... or rather declare dead something live, so it
    is indeterminate and the caller must refuse without operator action.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except (PermissionError, OSError):
        return None
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
    is_alive: Callable[[int], bool | None] = _process_live,
    reap: Callable[[int], None] = _reap_group,
) -> tuple[int, ...]:
    """Kill leftover agent children and verify each one dead. Unverifiable
    pids fail closed instead of being declared reaped; indeterminate liveness
    refuses recovery until an operator confirms the process is gone."""
    reaped: list[int] = []
    for pid in pids:
        pid = _validate_pid(pid)
        alive = is_alive(pid)
        if alive is None:
            raise RecoveryError(
                f"child process {pid} has indeterminate liveness; "
                "refusing without operator action"
            )
        if not alive:
            continue
        reap(pid)
        after_reap = is_alive(pid)
        if after_reap is None:
            raise RecoveryError(
                f"child process {pid} has indeterminate liveness after reaping; "
                "refusing without operator action"
            )
        if after_reap:
            raise RecoveryError(f"child process {pid} survived reaping; refusing")
        reaped.append(pid)
    return tuple(reaped)


def audit_core_trail(path: str | Path) -> bool:
    """True when the persisted core trail is unambiguous: no malformed lines,
    at most one `session_end`, and nothing substantive after it.

    A missing trail is unambiguous (nothing to contradict). A torn or
    foreign line is malformed and blocks recovery. Post-terminal telemetry
    (`turn_metrics`, `budget`) is bookkeeping the loop emits after closing
    the session — allowed. Anything else after the terminal (a second end,
    more model/tool traffic, a new start) is ambiguous and refuses.
    """
    try:
        text = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return True
    except OSError as exc:
        raise RecoveryError(f"unreadable event trail at {path}: {exc}") from exc
    kinds: list[str] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except ValueError:
            return False
        if not isinstance(event, dict) or not isinstance(event.get("type"), str):
            return False
        kinds.append(event["type"])
    ends = [i for i, kind in enumerate(kinds) if kind == "session_end"]
    if not ends:
        return True
    if len(ends) > 1:
        return False
    return all(kind in ("turn_metrics", "budget") for kind in kinds[ends[0] + 1 :])


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
    try:
        store.load_messages(session_id)
    except Exception as exc:
        raise RecoveryError(
            f"session {session_id} has no readable message checkpoint: {exc}"
        ) from exc
    active = unified.active
    if active.kind == "native" and active.native_session_id != session_id:
        raise RecoveryError("native runtime identity does not match the persisted session")
    if active.kind == "acp":
        try:
            from garuda.runtime.session import validate_authority_snapshot

            validate_authority_snapshot(active.capabilities)
        except Exception as exc:
            raise RecoveryError(
                f"ACP authority snapshot is missing or inconsistent: {exc}"
            ) from exc
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
    is_alive: Callable[[int], bool | None] = _process_live,
    reap: Callable[[int], None] = _reap_group,
) -> RecoveryReport:
    """Reap orphans, audit the persisted trail, classify, and mark rolled-back
    switches. Returns the session to resume; never invents success.

    The persisted event trail is reconstructed and audited before anything is
    selected: a malformed or ambiguous terminal trail refuses recovery even
    when the session meta alone looks resumable.
    """
    # The session record is the only source of signal targets.  Callers may
    # customise the liveness/reap operations for deterministic tests, but may
    # not smuggle an arbitrary PID into a production recovery operation.
    reaped = reap_orphans(_recorded_child_pids(store, session_id), is_alive=is_alive, reap=reap)
    events_path = getattr(store, "events_path", None)
    if callable(events_path):
        try:
            trail_ok = audit_core_trail(events_path(session_id))
        except RecoveryError:
            raise
        except Exception as exc:
            raise RecoveryError(
                f"session {session_id} trail audit failed: {exc}"
            ) from exc
        if not trail_ok:
            raise RecoveryError(
                f"session {session_id} has a malformed or ambiguous terminal "
                "trail; refusing without operator action"
            )
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
