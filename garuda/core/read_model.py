"""One read model of sessions, the queue, approvals and flows (plan task D.3, #167).

``garuda sessions --json`` and the dashboard's ``/api/sessions`` render these
functions' output and nothing else, so the two cannot disagree about a session's
state, outcome, verification or provenance. Everything here only *reads*: it takes
no lock, writes nothing, starts nothing, and a record it cannot read becomes a
visible ``unknown`` rather than an error or a guess.

The four independent facts of a session (``process``, ``work``, ``outcome`` and
``verification``) are reported as stored, with the process checked against its
recorded owner. ``crashed`` is derived (see ``runtime/session_state.py``). A
**review is not verification**: the flow ``review`` field is its own thing and
never feeds ``verification``. Cost and usage are not computed here (Set E owns
accounting); the row says ``unknown``.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from garuda.runtime.session_state import effective_state, is_crashed, summary_label

UNKNOWN = "unknown"


def _queue_index(queue) -> dict[str, dict]:
    """``{session_id: {state, position, scope}}``; ``{"": {...unknown}}`` marks an unreadable queue."""
    if queue is None:
        return {}
    try:
        entries = queue.entries()
    except Exception:
        return {"": {"state": UNKNOWN}}
    return {e["id"]: {"state": e["state"], "position": e["position"], "scope": e["scope"],
                      "harness": e.get("harness"), "quarantined": e.get("quarantined", False)}
            for e in entries}


def approvals(store, session_id: str) -> list[dict]:
    """Pending approval requests of one session (answered ones are not listed)."""
    from garuda.acp.approval_channel import FileApprovalChannel

    try:
        channel = FileApprovalChannel(Path(store.session_dir(session_id)) / "approvals",
                                      session_id)
        from garuda.context.redact import redact_text

        out = []
        for r in channel.pending():
            out.append({"approval_id": r.get("approval_id"),
                        "action": redact_text(str(r.get("action", "")))[0],
                        **{k: r.get(k) for k in ("family", "runtime_id", "expires_at", "ceiling",
                                                 "digest")},
                        "expired": time.time() > float(r.get("expires_at") or 0)})
        return out
    except Exception:
        return []


def _ref(ref: dict) -> dict:
    return {k: ref.get(k) for k in ("type", "digest", "producer_step", "producer_session",
                                    "attempt", "size")}


def _attempt(receipt: dict) -> dict:
    """One step attempt as a receipt recorded it. ``delta`` is whether the workspace
    version moved during the step; ``None`` when a version was not recorded."""
    before, after = receipt.get("workspace_version_before"), receipt.get("workspace_version_after")
    no_edits = receipt.get("no_edits") or {}
    return {
        "attempt": receipt.get("attempt"), "role": receipt.get("role"),
        "session_id": receipt.get("session_id") or receipt.get("session"),
        "status": receipt.get("status") or receipt.get("state"),
        "success": receipt.get("success"),
        "recorded_at": receipt.get("recorded_at"),
        "inputs": [_ref(r) for r in receipt.get("inputs") or []],
        "outputs": [_ref(r) for r in receipt.get("outputs") or []],
        "members": [{k: m.get(k) for k in ("role", "session_id", "success")}
                    for m in receipt.get("members") or []],
        "delta": None if before is None or after is None else before != after,
        "no_edits": no_edits.get("result"),
        "stop": receipt.get("stop"),
    }


def inbox(store, *, limit: int = 200) -> list[dict]:
    """Every pending approval across active sessions, oldest session first."""
    from garuda.runtime.session_state import ACTIVE_WORK

    out = []
    for meta in reversed(store.list_sessions(limit=limit)):
        session_id = meta.get("session_id") or ""
        if effective_state(meta).get("work") not in ACTIVE_WORK:
            continue
        for request in approvals(store, session_id):
            out.append({"session_id": session_id, "name": meta.get("name"),
                        "task": meta.get("task"), **request})
    return out


def flow(store, session_id: str, meta: dict | None = None) -> dict | None:
    """A flow session's steps, attempts, receipts and review outcome, or ``None``."""
    meta = meta if meta is not None else store.load_meta(session_id)
    if meta.get("kind") != "flow":
        return None
    from garuda.flows import engine

    declared = (meta.get("flow") or {}).get("steps") or []
    try:
        receipts = engine.receipts(store, session_id)
    except Exception:
        receipts = []
    steps = []
    edges = []
    for step in declared:
        attempts = [_attempt(r) for r in receipts if r.get("step") == step]
        for attempt in attempts:
            for ref in attempt["inputs"]:
                edge = {"from_step": ref["producer_step"], "to_step": step, "type": ref["type"],
                        "digest": ref["digest"]}
                if edge not in edges:
                    edges.append(edge)
        steps.append({"id": step, "attempts": attempts})
    return {"name": (meta.get("flow") or {}).get("name"), "state": meta.get("flow_state"),
            "steps": steps, "edges": edges,
            # Own field: a review verdict is advice about the work, not verification of it.
            "review": meta.get("review")}


def session_row(store, meta: dict, *, queue_index: dict | None = None) -> dict[str, Any]:
    """The shared row for one session."""
    session_id = meta.get("session_id") or ""
    state = effective_state(meta)
    verification = dict(state.get("verification") or {"status": UNKNOWN})
    segments = meta.get("runtime_segments") or []
    runtime = segments[-1].get("runtime_id") if segments and isinstance(segments[-1], dict) \
        else None
    queue = (queue_index or {}).get(session_id)
    if queue is None and queue_index is not None and "" in queue_index:
        queue = {"state": UNKNOWN}
    worker = meta.get("worker") if isinstance(meta.get("worker"), dict) else None
    return {
        "session_id": session_id,
        "name": meta.get("name"),
        "project_id": meta.get("project_id"),
        "kind": meta.get("kind") or "session",
        "task": meta.get("task"),
        "agent": meta.get("agent"),
        "agent_digest": meta.get("agent_digest"),
        "agent_segment": meta.get("agent_segment"),
        "model": meta.get("model"),
        "runtime": runtime or "native",
        "role": meta.get("role"),
        "status": meta.get("status"),
        "state": {k: state.get(k) for k in ("process", "work", "outcome")},
        "label": summary_label(state),
        "crashed": is_crashed(state),
        "verification": verification,
        "self_check": state.get("self_check"),
        "workspace": meta.get("workspace"),
        "isolation": meta.get("isolation") or "shared",
        "branch": meta.get("branch"),
        "queue": queue,
        "worker": {"pid": worker.get("pid")} if worker else None,
        "resumed_from": meta.get("resumed_from"),
        "usage": UNKNOWN,
        "cost": UNKNOWN,
        "created_at": meta.get("created_at"),
        "updated_at": meta.get("updated_at"),
    }


def sessions(store, *, limit: int | None = 20, queue=None) -> list[dict]:
    index = _queue_index(queue)
    rows = []
    for meta in store.list_sessions(limit=limit):
        row = session_row(store, meta, queue_index=index)
        row["approvals_pending"] = len(approvals(store, row["session_id"])) \
            if row["state"]["work"] in ("queued", "working", "waiting") else 0
        rows.append(row)
    return rows


def session(store, session_id: str, *, queue=None) -> dict:
    meta = store.load_meta(session_id)
    row = session_row(store, meta, queue_index=_queue_index(queue))
    row["approvals"] = approvals(store, session_id)
    row["approvals_pending"] = len(row["approvals"])
    row["flow"] = flow(store, session_id, meta)
    return row
