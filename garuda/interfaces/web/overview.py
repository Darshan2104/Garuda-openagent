"""The Overview page's one read: what is happening, and does anything need you.

``/api/sessions`` orders rows by ``updated_at`` and returns at most ``reads.MAX_LIMIT``
(500; 100 by default), and a session
whose worker died stops updating — so the crashed run, or the one parked on an
approval, is exactly the row that falls off the end of that list. This read
therefore selects from *every* saved session, server side, and builds full rows
only for the few it returns:

* **attention**: sessions whose worker crashed, sessions waiting on an approval,
  and flows stopped on a resumable pause (a quota wait recorded by quota recovery,
  or a step's own deadline, silence, progress, turn or refusal limit);
* **running**: queued, working and waiting sessions that did not crash, with a
  flow's in-flight step when its journal names one;
* **recent**: the newest finished sessions.

Every row is the shared read model's (``core.read_model.session_row``), trimmed to
the fields the page renders; nothing here is computed that the read model or the
flow engine's own read-only helpers do not already expose. Like the rest of the
read model it takes no lock, writes nothing and starts nothing.
"""

from __future__ import annotations

import time
from typing import Any

from garuda.core import read_model

#: How many finished sessions the page lists.
RECENT = 8
#: A ceiling per list, so a store with hundreds of crashed runs still answers with a
#: bounded body. Each list also reports its full ``total``.
LIST_CAP = 50

#: The row fields the page renders, all copied from ``session_row``.
ROW_FIELDS = (
    "session_id", "name", "kind", "origin", "background", "runtime", "model", "label",
    "crashed", "pause", "state", "verification", "self_check", "queue", "quota_recovery",
    "created_at", "updated_at", "flow_step",
)

#: What a running flow's row carries from the shared live view (``read_model.flow_in_flight``).
IN_FLIGHT_FIELDS = ("step", "attempt", "session_id", "turns", "last_activity_at", "idle_sec",
                    "started_at", "paused", "silence_state", "silence_limit_sec",
                    "tool_silence_limit_sec", "tool_in_flight", "approval_parked")

# The pause facts live in the shared read model, so the Overview, the session row and
# `garuda sessions --json` report the same pause. The old names stay importable here.
_pause = read_model.flow_pause
_pause_kinds = read_model.pause_kinds
quota_limit = read_model.quota_limit
QUOTA_STOP = read_model.QUOTA_STOP


def _progress(store, meta: dict) -> dict | None:
    """The step a running flow has launched and not yet settled, from its journal (the
    shared read model's, which a session's detail reports as ``flow_progress``)."""
    from garuda.core.read_model import flow_progress

    return flow_progress(store, meta)


def _row(store, meta: dict, queue_index: dict, *, active: bool) -> dict[str, Any]:
    from garuda.core import read_model

    full = read_model.session_row(store, meta, queue_index=queue_index)
    row = {key: full.get(key) for key in ROW_FIELDS}
    role = full.get("role")
    row["role"] = {"name": role.get("name")} if isinstance(role, dict) else None
    if active:
        pending = read_model.approvals(store, row["session_id"])
        row["approvals_pending"] = len(pending)
        row["approvals_live"] = sum(1 for a in pending if not a.get("expired"))
    if row["kind"] == "flow":
        flow = read_model.flow(store, row["session_id"], meta)
        row["flow"] = {"name": flow.get("name"), "state": flow.get("state"),
                       "stop": flow.get("stop")} if flow else None
        live = flow.get("in_flight") if flow else None
        row["in_flight"] = ({k: live.get(k) for k in IN_FLIGHT_FIELDS}
                            if isinstance(live, dict) else None)
    return row


def _nest_steps(running: list[dict]) -> list[dict]:
    """A running flow's in-flight step session listed under its flow, not beside it.

    Only recorded links: the step session's own ``flow_step.flow_session``, or the flow's
    live view naming the step session (``in_flight.session_id``). A step session whose flow
    is not itself listed as running stays a row of its own."""
    flows = {row["session_id"]: row for row in running if row.get("kind") == "flow"}
    by_child = {row["in_flight"]["session_id"]: row for row in flows.values()
                if isinstance(row.get("in_flight"), dict) and row["in_flight"].get("session_id")}
    top = []
    for row in running:
        link = row.get("flow_step") or {}
        parent = flows.get(link.get("flow_session")) or by_child.get(row["session_id"])
        if parent is not None and parent is not row:
            parent.setdefault("children", []).append(row)
        else:
            top.append(row)
    return top


def _listing(rows: list[dict]) -> dict[str, Any]:
    return {"items": rows[:LIST_CAP], "total": len(rows)}


def overview(store, *, queue=None, now: float | None = None) -> dict[str, Any]:
    from garuda.core import read_model
    from garuda.runtime.session_state import ACTIVE_WORK, effective_state, is_crashed

    now = time.time() if now is None else now
    metas = store.list_sessions(limit=None)  # newest first, every session
    # The same queue index ``read_model.sessions`` builds, so a queue position here and
    # on the Sessions page cannot disagree; an unreadable queue marks rows ``unknown``.
    index = read_model._queue_index(queue)
    crashed: list[dict] = []
    running: list[dict] = []
    waiting: list[dict] = []
    paused: list[dict] = []
    recent: list[dict] = []
    approvals = {"waiting": 0, "expired": 0, "sessions": 0}
    for meta in metas:
        state = effective_state(meta)
        if state.get("work") in ACTIVE_WORK:
            row = _row(store, meta, index, active=True)
            if is_crashed(state):
                crashed.append(row)
            else:
                running.append(row)
                if meta.get("kind") == "flow":
                    row["progress"] = _progress(store, meta)
            if row["approvals_live"]:
                approvals["sessions"] += 1
            # Counted like the inbox counts them; a crashed session is listed once, as crashed.
            approvals["waiting"] += row["approvals_live"]
            approvals["expired"] += row["approvals_pending"] - row["approvals_live"]
            if not row["crashed"] and (row["approvals_live"] or state.get("work") == "waiting"):
                waiting.append(row)
            continue
        pause = _pause(meta)
        if pause is not None:
            row = _row(store, meta, index, active=False)
            row["pause"] = pause
            paused.append(row)
        if len(recent) < RECENT:
            recent.append(row if pause is not None else _row(store, meta, index, active=False))
    return {
        "generated_at": now,
        "sessions_total": len(metas),
        "approvals": approvals,
        "attention": {"crashed": _listing(crashed), "waiting": _listing(waiting),
                      "paused": _listing(paused)},
        "running": _listing(_nest_steps(running)),
        "recent": recent,
    }
