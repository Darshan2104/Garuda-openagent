"""Four independent facts about a session (plan task B.2, issue #157).

A session's single ``status`` word used to stand for several different things
at once: whether its process is running, what its work is doing, how it ended
and whether its result was checked. ``running`` stayed ``running`` forever for
a killed run, and ``success`` could not say whether anyone verified the result.
The session record now keeps them apart:

==============  ============================================================
``process``     ``starting``, ``live``, ``exited``, ``missing``, ``unknown``
``work``        ``queued``, ``working``, ``waiting``, ``done``, ``stopped``
``outcome``     unset while work is active; then ``completed``, ``failed``,
                ``refused`` or ``cancelled``
``verification`` ``passed``, ``failed``, ``unavailable`` or ``invalidated``,
                with the authority that produced it
==============  ============================================================

A **self-check** — the native completion gate accepting the agent's own
verification commands — is recorded separately and never counts as
verification. Only an authoritative grader (and, later, trusted acceptance
checks) can make verification ``passed``.

``crashed`` is never stored. :func:`is_crashed` derives it: the process has
exited or gone missing while the work is still active and no outcome was
recorded. The legacy ``status`` field is still written exactly as before, so
existing readers, exit codes and APIs are unchanged.
"""

from __future__ import annotations

from typing import Any, Callable

STATE_VERSION = 1
PROCESS = ("starting", "live", "exited", "missing", "unknown")
WORK = ("queued", "working", "waiting", "done", "stopped")
ACTIVE_WORK = ("queued", "working", "waiting")
OUTCOME = ("completed", "failed", "refused", "cancelled")
VERIFICATION = ("passed", "failed", "unavailable", "invalidated")
SELF_CHECK = ("passed", "failed")


class SessionStateError(ValueError):
    """A state that cannot happen (for example an outcome while work is active)."""


def validate(state: dict) -> dict:
    """Return ``state`` if it is a possible combination, else raise."""
    if not isinstance(state, dict):
        raise SessionStateError("session state must be a mapping")
    process, work, outcome = state.get("process"), state.get("work"), state.get("outcome")
    if process not in PROCESS:
        raise SessionStateError(f"process must be one of {PROCESS}, got {process!r}")
    if work not in WORK:
        raise SessionStateError(f"work must be one of {WORK}, got {work!r}")
    if work in ACTIVE_WORK and outcome is not None:
        raise SessionStateError(f"active work ({work}) cannot have an outcome ({outcome})")
    if work not in ACTIVE_WORK and outcome not in OUTCOME:
        raise SessionStateError(f"finished work ({work}) needs an outcome in {OUTCOME}")
    if work == "stopped" and outcome not in ("cancelled", "refused"):
        raise SessionStateError("stopped work ends cancelled or refused")
    verification = state.get("verification") or {}
    status = verification.get("status")
    if status not in VERIFICATION:
        raise SessionStateError(f"verification must be one of {VERIFICATION}, got {status!r}")
    if status in ("passed", "failed") and not verification.get("authority"):
        raise SessionStateError("a passed or failed verification must name its authority")
    self_check = state.get("self_check")
    if self_check is not None and self_check.get("status") not in SELF_CHECK:
        raise SessionStateError(f"self_check must be one of {SELF_CHECK}")
    return state


def started(owner: dict | None = None) -> dict:
    """A session whose process is running and whose work has begun."""
    state = {
        "version": STATE_VERSION,
        "process": "live",
        "work": "working",
        "outcome": None,
        "verification": {"status": "unavailable"},
        "self_check": None,
    }
    if owner:
        state["owner"] = owner
    return validate(state)


def queued(owner: dict | None = None) -> dict:
    """A background session whose worker is waiting for its turn (D.2): the process
    exists but no work has begun."""
    state = {
        "version": STATE_VERSION,
        "process": "live",
        "work": "queued",
        "outcome": None,
        "verification": {"status": "unavailable"},
        "self_check": None,
    }
    if owner:
        state["owner"] = owner
    return validate(state)


def finished(*, success: bool, completion_gate: dict | None = None) -> dict:
    """The terminal state of a run that ended normally.

    ``completion_gate`` is the run's ``metadata["completion_gate"]``: whether
    the native gate ran, and whether an authoritative grader stood behind it.
    """
    gate = completion_gate or {}
    verification: dict[str, Any] = {"status": "unavailable"}
    if success and gate.get("authoritative_grader"):
        verification = {"status": "passed", "authority": "user-config"}
    self_check = None
    if gate.get("verifier"):
        self_check = {"status": "passed" if success else "failed", "source": "native-completion-gate"}
    return validate(
        {
            "version": STATE_VERSION,
            "process": "exited",
            "work": "done",
            "outcome": "completed" if success else "failed",
            "verification": verification,
            "self_check": self_check,
        }
    )


def interrupted(*, cancelled: bool = False) -> dict:
    """A run that ended without a result: cancelled, or failed before finishing."""
    return validate(
        {
            "version": STATE_VERSION,
            "process": "exited",
            "work": "stopped" if cancelled else "done",
            "outcome": "cancelled" if cancelled else "failed",
            "verification": {"status": "unavailable"},
            "self_check": None,
        }
    )


#: How each legacy ``status`` reads in the four fields. Unknown values keep
#: their raw status and claim nothing about the outcome beyond "not completed".
LEGACY = {
    "running": ("unknown", "working", None),
    "success": ("exited", "done", "completed"),
    "completed": ("exited", "done", "completed"),
    "finished": ("exited", "done", "completed"),
    "failed": ("exited", "done", "failed"),
}


def from_legacy(meta: dict) -> dict:
    """The four fields for a record written before they existed."""
    raw = meta.get("status")
    process, work, outcome = LEGACY.get(raw, ("unknown", "done", "failed"))
    state = {
        "version": STATE_VERSION,
        "process": process,
        "work": work,
        "outcome": outcome,
        "verification": {"status": "unavailable"},
        # Legacy native `success` meant the completion gate accepted the run.
        "self_check": {"status": "passed", "source": "legacy-status"} if raw == "success" else None,
        "legacy_status": raw,
    }
    return validate(state)


def _worker_owner(meta: dict) -> dict | None:
    """A background session's worker (D.2) as an owner, for the moments before it
    has recorded its own: the launcher wrote its pid and start identity."""
    worker = meta.get("worker")
    if isinstance(worker, dict) and isinstance(worker.get("pid"), int) and worker.get("identity"):
        return {"pid": worker["pid"], "identity": worker["identity"],
                "pgid": worker.get("pgid", worker["pid"]), "epoch": ""}
    return None


def effective_state(meta: dict, *, liveness: Callable[[dict], bool | None] | None = None) -> dict:
    """The session's state as of now, with process liveness checked.

    A stored ``live`` process whose recorded owner is confirmed dead reads as
    ``missing``; one that cannot be checked reads as ``unknown``.
    """
    stored = meta.get("state")
    try:
        state = dict(validate(dict(stored))) if isinstance(stored, dict) else from_legacy(meta)
    except SessionStateError:
        state = from_legacy(meta)
    if state["process"] in ("starting", "live"):
        owner = state.get("owner") or _worker_owner(meta)
        if owner is None:
            state["process"] = "unknown"
        else:
            if liveness is None:
                from garuda.runtime.ownership import owner_liveness as liveness
            alive = liveness(owner)
            state["process"] = "live" if alive else ("missing" if alive is False else "unknown")
    return state


def is_crashed(state: dict) -> bool:
    """Derived, never stored: the process is gone but the work never ended."""
    return (
        state.get("process") in ("exited", "missing")
        and state.get("work") in ACTIVE_WORK
        and state.get("outcome") is None
    )


def summary_label(state: dict) -> str:
    """One word for a list view; storage and APIs keep the four fields."""
    if is_crashed(state):
        return "crashed"
    if state.get("work") in ACTIVE_WORK:
        return state["work"]
    return state.get("outcome") or state.get("work", "?")
