"""A checked flow waits out a Claude usage limit in the process that runs it.

An unchecked flow whose Claude step hits its usage limit stops with
``flow.quota_wait`` and the reset Claude reported (#834); quota recovery's tick
continues it :data:`~garuda.flows.quota_recovery.CLAUDE_RESET_MARGIN_SEC` after
that reset. Quota recovery never runs a checked flow (``--check``): only
``garuda flow resume`` restores its original check request. So the
``garuda flow run --check`` (or ``garuda flow resume``) process that settled the
stop waits for the reset itself, with the same margin, then continues the flow
exactly as ``garuda flow resume`` does: from the settled stop's checkpoint, under
the original checks, with the next attempt of the same step.

Only a settled stop qualifies (:func:`pending`): a stopped, CLI-checked flow with
its resume checkpoint, whose last journal entry is the receipt of a Claude step
attempt that stopped with ``flow.quota_wait``, whose worker and every descendant
were proved closed (the owned worker's ECHILD receipt) and whose evidence boundary
was taken, and whose cause is a typed usage limit with a reset still in the
future. Anything else (no reported reset, a reset already passed, a dashboard
flow, an attempt that never settled, a tree not proved closed) is left as the
plain resumable or quarantined stop it already is. A repeated limit waits again
only for a strictly later reset, so a stale reset can never loop.

While it waits the process records itself as ``flow_quota_wait`` in the flow's
meta (its owner, the reset and when it continues); the read model shows the
pause as resuming automatically while that owner is alive, and ``garuda flow
resume`` refuses beside it. If the process is killed while it waits, the stop is
still the settled, resumable ``flow.quota_wait``: ``garuda flow resume``
continues it (waiting for the reset first when it has not passed yet).
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

#: The flow meta key of a live in-process wait.
WAIT_KEY = "flow_quota_wait"
QUOTA_STOP = "flow.quota_wait"
#: The longest single sleep, so a suspended host or a clock change is noticed.
POLL_SEC = 60.0
#: The sleep the wait uses (tests replace it with a clock of their own).
_sleep = asyncio.sleep
_PROTECTED = ("flow_operation", "background_operation", "flow_verification_phase",
              "flow_verification_commit", "acceptance_receipts", "flow_evidence_manifest")


@dataclass(frozen=True)
class Pause:
    """A settled checked usage-limit stop this process waits out."""

    flow_session: str
    step: str
    attempt: int
    reset_at: int
    rate_limit_type: str | None
    #: The reset plus the margin quota recovery uses for an unchecked flow.
    resume_at: float
    #: The id of the stop's ``flow_resume_checkpoint``: the wait is bound to it.
    checkpoint: str

    def record(self, owner: dict) -> dict:
        return {"step": self.step, "attempt": self.attempt, "reset_at": self.reset_at,
                "rate_limit_type": self.rate_limit_type, "resume_at": self.resume_at,
                "checkpoint": self.checkpoint, "owner": owner}


def _closed(receipt: dict) -> bool:
    """The attempt's owned worker and every descendant were proved closed (ECHILD) and
    its evidence boundary was taken; the engine settles a checked receipt only so."""
    closure = receipt.get("closure")
    process = closure.get("process") if isinstance(closure, dict) else None
    return (isinstance(process, dict)
            and process.get("cleanup") == {"backend": "linux-subreaper", "echild": True}
            and process.get("outcome") == "exited"
            and isinstance(receipt.get("evidence_boundary"), dict))


def _settled(store, flow_session: str) -> Pause | None:
    """The settled checked limit stop, whatever its reset time, else ``None``. Read only."""
    from garuda.acp.protocol import rate_limit_fields
    from garuda.flows import engine
    from garuda.flows.quota_recovery import CLAUDE_RESET_MARGIN_SEC, CLAUDE_RUNTIME

    try:
        meta = store.load_meta(flow_session)
        rows = engine.receipts(store, flow_session)
        entries = engine.journal(store, flow_session)
        unsettled = engine.unsettled_attempts(store, flow_session)
    except Exception:
        return None
    if not isinstance(meta, dict):
        return None
    policy = meta.get("publication_policy")
    stop = meta.get("flow_stop") if isinstance(meta.get("flow_stop"), dict) else {}
    checkpoint = meta.get("flow_resume_checkpoint")
    if not (meta.get("flow_state") == "stopped" and meta.get("status") == "failed"
            and isinstance(policy, dict) and policy.get("kind") == "flow-verification"
            and not any(key in meta for key in _PROTECTED)
            and isinstance(checkpoint, dict) and isinstance(checkpoint.get("id"), str)
            and stop.get("code") == QUOTA_STOP and isinstance(stop.get("step"), str)
            and not unsettled and entries and entries[-1].get("event") == "receipt"):
        return None
    last = entries[-1]
    receipt = next((r for r in rows if (r.get("step"), r.get("attempt"))
                    == (last.get("step"), last.get("attempt"))), {})
    recorded = receipt.get("stop") if isinstance(receipt.get("stop"), dict) else {}
    cause = recorded.get("cause") if isinstance(recorded.get("cause"), dict) else {}
    if not (receipt.get("step") == stop["step"] and type(receipt.get("attempt")) is int
            and receipt.get("status") == "stopped" and recorded.get("code") == QUOTA_STOP
            and (receipt.get("role_plan") or {}).get("runtime_id") == CLAUDE_RUNTIME
            and cause.get("code") in engine.LIMIT_CAUSE_CODES and _closed(receipt)):
        return None
    limit = rate_limit_fields(cause.get("limit"), validated=True)
    reset = limit.get("resets_at")
    if reset is None:
        return None
    return Pause(flow_session, stop["step"], receipt["attempt"], reset,
                 limit.get("rate_limit_type"), float(reset + CLAUDE_RESET_MARGIN_SEC),
                 checkpoint["id"])


def pending(store, flow_session: str, *, after: int | None = None,
            now: float | None = None) -> Pause | None:
    """The settled checked usage-limit stop this process may wait out, else ``None``.

    Its reported reset must still be in the future and, after an earlier wait
    (``after``, that wait's reset), strictly later than it. Read only.
    """
    pause = _settled(store, flow_session)
    now = time.time() if now is None else now
    if pause is None or pause.reset_at <= now or (after is not None and pause.reset_at <= after):
        return None
    return pause


def live_waiter(meta: dict) -> dict | None:
    """The ``flow_quota_wait`` record of a process alive and waiting, else ``None``."""
    from garuda.runtime.ownership import owner_liveness

    record = meta.get(WAIT_KEY) if isinstance(meta, dict) else None
    owner = record.get("owner") if isinstance(record, dict) else None
    if not isinstance(owner, dict):
        return None
    alive = owner_liveness({key: owner.get(key) for key in ("pid", "identity", "pgid")})
    return record if alive is True else None


def _clear(store, flow_session: str, owner: dict) -> None:
    """Remove this process's own wait record; another waiter's is left alone."""
    def mutate(meta):
        record = meta.get(WAIT_KEY)
        mine = isinstance(record, dict) and (record.get("owner") or {}).get("epoch") == owner["epoch"]
        return {WAIT_KEY: None} if mine else {}

    try:
        store.mutate_meta(flow_session, mutate)
    except Exception:
        pass  # a stale record names a dead owner, which reads as not waiting


async def wait(store, pause: Pause, notify=None) -> bool:
    """Sleep until ``pause.resume_at``; True when the same settled stop is still there.

    Cancelled (Ctrl-C, SIGTERM), it returns False and leaves the settled stop for
    ``garuda flow resume``. It never launches anything itself.
    """
    from garuda.runtime.ownership import current_owner

    owner = current_owner().to_dict()
    await asyncio.to_thread(store.update_meta, pause.flow_session, {WAIT_KEY: pause.record(owner)})
    if notify is not None:
        notify(pause)
    try:
        while (left := pause.resume_at - time.time()) > 0:
            await _sleep(min(left, POLL_SEC))
        same = await asyncio.to_thread(_settled, store, pause.flow_session)
    except asyncio.CancelledError:
        return False
    finally:
        _clear(store, pause.flow_session, owner)
    # Continued or changed meanwhile (a manual resume, a cancel): never continue it twice.
    return same == pause
