"""One read-only view of who owns a session's work and what blocks reconciling it (#376).

The facts already live with separate durable owners: the session's recorded
owner and liveness (``runtime/session_state.py``), its retained workspace
leases (``workspace/lease.py``), its queue entry or claim (``runtime/queue.py``),
its capacity slots (``runtime/capacity.py``), its recorded runtime children
(``runtime/recovery.py``), pending approvals and quota recovery. This module
joins them for one session so ``garuda sessions show --json`` and
``/api/sessions/<id>`` answer "who owns this, what is it waiting on, and can it
be reconciled" without the reader stitching records together.

Everything here only *reads*. It takes no lock, writes nothing, starts nothing
and sends no signal; the owner liveness it reports is the one the session row
already derived, and no other process is probed. A record it cannot read makes
that part ``unavailable`` and reconciliation ``unknown`` — never a guess.

**Reconciliation is advice, not action.** ``eligible`` names the one existing
owner-proved path (``garuda runtime recover``), which re-proves every
prerequisite itself before it signals anything. Retained leases, quarantined
queue claims and unknown owner liveness block it here, as they would there;
retained capacity slots are reported because no path releases them
automatically. Inspection never authorizes replay.
"""

from __future__ import annotations

from typing import Any

from garuda.runtime.session_state import ACTIVE_WORK

SCHEMA = "garuda.ownership-projection"
SCHEMA_VERSION = 1
MAX_CHILDREN = 32
UNKNOWN = "unknown"

#: How the derived process fact reads as owner liveness.
_LIVENESS = {"live": "alive", "missing": "dead", "unknown": UNKNOWN, "exited": "exited"}
#: Quota recovery states in which its scheduler, not this session's process, owns continuation.
_QUOTA_OWNED = frozenset({"waiting-for-quota", "dispatch-reserved", "running"})
_QUOTA_ATTENTION = frozenset({"blocked", "uncertain"})
RECOVER_COMMAND = "garuda runtime recover --session {session_id}"


def _int_or_none(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _owner(meta: dict, process: str | None) -> dict:
    state = meta.get("state") if isinstance(meta.get("state"), dict) else {}
    recorded, source = state.get("owner"), "state"
    if not isinstance(recorded, dict):
        recorded, source = meta.get("worker"), "worker"
    if not isinstance(recorded, dict):
        recorded, source = {}, None
    return {"pid": _int_or_none(recorded.get("pid")), "source": source,
            "liveness": _LIVENESS.get(process or "", UNKNOWN)}


def _children(meta: dict) -> dict:
    """Recorded runtime children as stored; their processes are not probed here."""
    children = meta.get("runtime_children", [])
    if not isinstance(children, list):
        return {"coverage": "unavailable", "entries": [], "omitted": 0,
                "reason": "runtime children record is not a list"}
    entries = []
    for child in children[:MAX_CHILDREN]:
        if not isinstance(child, dict):
            return {"coverage": "unavailable", "entries": [], "omitted": 0,
                    "reason": "runtime child record is not a mapping"}
        entries.append({"pid": _int_or_none(child.get("pid")),
                        "runtime_id": child.get("runtime_id"),
                        "state": child.get("state", "live"),
                        "process": UNKNOWN})
    return {"coverage": "complete", "entries": entries,
            "omitted": max(0, len(children) - MAX_CHILDREN)}


def _leases(session_id: str) -> dict:
    from garuda.workspace.lease import LeaseError, LeaseStore

    try:
        holders = LeaseStore().live_holders_for_session(session_id)
    except (LeaseError, OSError) as exc:
        return {"coverage": "unavailable", "holders": [], "reason": str(exc)}
    return {"coverage": "complete", "holders": [
        {"workspace": h.workspace, "mode": h.mode, "pid": h.pid,
         "heartbeat_at": h.heartbeat_at, "ttl_sec": h.ttl_sec,
         "heartbeat_expired": h.is_stale()} for h in holders]}


def _queue(session_id: str, entries: list[dict] | None, configured: bool) -> dict:
    if not configured:
        return {"coverage": "not_configured", "entry": None}
    if entries is None:
        return {"coverage": "unavailable", "entry": None}
    entry = next((e for e in entries if e.get("id") == session_id
                  or e.get("session_id") == session_id), None)
    if entry is None:
        return {"coverage": "complete", "entry": None}
    worker = entry.get("worker") if isinstance(entry.get("worker"), dict) else {}
    return {"coverage": "complete", "entry": {
        "state": entry.get("state"), "scope": entry.get("scope"),
        "position": entry.get("position"), "harness": entry.get("harness"),
        "quarantined": bool(entry.get("quarantined")),
        "pending_operation": entry.get("pending_operation"),
        "heartbeat_at": entry.get("heartbeat"),
        "worker_pid": _int_or_none(worker.get("pid"))}}


def _capacity(session_id: str, queue) -> dict:
    from garuda.runtime.capacity import CapacityError, CapacityStore

    try:
        # The queue's store when there is one, so both read the same root.
        capacity = queue.capacity if queue is not None else CapacityStore()
        slots = capacity.slots_for_holder(session_id)
    except (CapacityError, OSError, ValueError) as exc:
        return {"coverage": "unavailable", "slots": [], "reason": str(exc)}
    out = []
    for slot in slots:
        marker = slot.get("queue") if isinstance(slot.get("queue"), dict) else None
        owner = slot.get("owner") if isinstance(slot.get("owner"), dict) else {}
        out.append({"key": slot["key"],
                    "kind": "queue" if marker else ("loan" if "loan" in slot else "ordinary"),
                    "phase": marker.get("phase") if marker else None,
                    "owner_pid": _int_or_none(owner.get("pid")),
                    "reserved_at": slot.get("reserved_at")})
    return {"coverage": "complete", "slots": out}


def _waiting(row: dict, approvals_pending: int, queue: dict, quota: dict | None) -> list[dict]:
    out = []
    entry = queue.get("entry")
    if entry and entry["quarantined"]:
        out.append({"reason": "quarantine", "operation": entry["pending_operation"]})
    elif entry and entry["state"] == "queued":
        out.append({"reason": "queue", "position": entry["position"]})
    if approvals_pending:
        out.append({"reason": "approval", "count": approvals_pending})
    if quota and quota.get("state") == "waiting-for-quota":
        out.append({"reason": "quota", "next_check": quota.get("next_check")})
    if row["state"].get("work") == "waiting" and not out:
        out.append({"reason": UNKNOWN})
    return out


def _reconciliation(session_id: str, row: dict, owner: dict, leases: dict, queue: dict,
                    capacity: dict, quota: dict | None, workers: dict) -> dict:
    active = row["state"].get("work") in ACTIVE_WORK
    blockers, retained = [], []
    if leases["coverage"] != "complete":
        blockers.append("workspace_leases_unreadable")
    elif leases["holders"]:
        blockers.append("workspace_lease_retained")
        retained.append("workspace_lease")
    if queue["coverage"] == "unavailable":
        blockers.append("queue_unreadable")
    entry = queue.get("entry")
    if entry and entry["quarantined"]:
        blockers.append("queue_claim_quarantined")
        retained.append("queue_claim")
    elif entry and entry["state"] == "running" and owner["liveness"] != "alive":
        blockers.append("queue_claim_retained")
        retained.append("queue_claim")
    if capacity["coverage"] == "unavailable":
        blockers.append("capacity_unreadable")
    elif capacity["slots"]:
        retained.append("capacity_slot")
    if workers["coverage"] != "complete":
        blockers.append("editing_workers_unreadable")
    elif any(e["repository_state"] != "removed" for e in workers["entries"]):
        blockers.append("editing_worker_retained")
        retained.append("editing_worker")
    quota_state = (quota or {}).get("state")
    result: dict[str, Any] = {"status": "not_needed", "blockers": [], "retained": retained,
                              "path": None, "performs_repair": False}
    if quota_state in _QUOTA_OWNED:
        result.update(status="owner_scheduled", owner="quota-recovery",
                      path={"command": "garuda recovery status"})
        return result
    if quota_state in _QUOTA_ATTENTION:
        # An uncertain dispatch refuses automatic replay; nothing here revives it.
        result.update(status="blocked", blockers=[f"quota_recovery_{quota_state}", *blockers],
                      path={"command": "garuda recovery status"})
        return result
    unreadable = any(b.endswith("_unreadable") for b in blockers)
    if not active:
        if unreadable:
            result.update(status=UNKNOWN, blockers=blockers)
        elif retained:
            # Finished work that still holds ownership: only its issuing owner
            # (or an operator) may release it; nothing here proposes a takeover.
            result.update(status="blocked", blockers=[f"{r}_retained" for r in retained])
        return result
    if owner["liveness"] == "alive":
        result.update(status="owner_running")
        return result
    if owner["liveness"] == UNKNOWN or unreadable:
        result.update(status=UNKNOWN, blockers=(["owner_liveness_unknown"]
                                                if owner["liveness"] == UNKNOWN else []) + blockers)
        return result
    if any(b in blockers for b in ("workspace_lease_retained", "queue_claim_quarantined",
                                   "queue_claim_retained", "editing_worker_retained")):
        result.update(status="blocked", blockers=blockers)
        return result
    result.update(status="eligible", path={
        "command": RECOVER_COMMAND.format(session_id=session_id),
        "rechecks": ["workspace leases", "owner processes", "event trail",
                     "child process identities"]})
    return result


def project(row: dict, meta: dict, *, approvals_pending: int, queue=None,
            queue_entries: list[dict] | None = None) -> dict:
    """The ownership projection for one already-loaded session row.

    ``queue_entries`` is the caller's single read of ``queue`` (``None`` when it
    was unreadable), so the row and this projection describe the same snapshot.
    """
    session_id = row["session_id"]
    owner = _owner(meta, row["state"].get("process"))
    queue_view = _queue(session_id, queue_entries, queue is not None)
    lease_view = _leases(session_id)
    capacity_view = _capacity(session_id, queue)
    quota = meta.get("quota_recovery") if isinstance(meta.get("quota_recovery"), dict) else None
    from garuda.workspace.editing_workers import EditingWorkerRepositories

    workers = EditingWorkerRepositories().inventory(session_id)
    entry = queue_view.get("entry") or {}
    return {
        "schema": SCHEMA,
        "version": SCHEMA_VERSION,
        "owner": owner,
        "lineage": {"resumed_from": meta.get("resumed_from"), "children": _children(meta)},
        "progress": {
            "work": row["state"].get("work"),
            "operation": entry.get("pending_operation") or (quota or {}).get("state"),
            "recorded_at": meta.get("updated_at"),
            "queue_heartbeat_at": entry.get("heartbeat_at"),
            "budget": UNKNOWN,
            "waiting": _waiting(row, approvals_pending, queue_view, quota),
        },
        "workspace_leases": lease_view,
        "queue": queue_view,
        "capacity": capacity_view,
        "editing_workers": workers,
        "reconciliation": _reconciliation(session_id, row, owner, lease_view, queue_view,
                                          capacity_view, quota, workers),
    }
