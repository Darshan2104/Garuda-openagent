"""Approvals waiting in this dashboard's broker for the flows and runs it started.

A flow or run started from the dashboard (an owned starter operation) asks its approvals
through the in-process :class:`~garuda.interfaces.web.approvals.ApprovalBroker`, owned by
the operation id, not through a session's file channel. Those asks are not in the shared
read model (``read_model.inbox``, which the CLI also reads), because they live only in
this process; this module lists them for the Approvals page, the nav badge and the
Overview, so any tab of this dashboard sees them.

Listing never refreshes an owner's heartbeat and never answers. Answering keeps the
broker's rule: ``POST /api/approvals/<ask_id>`` with the ask's owner and digest, which this
listing carries, so any tab that was shown the ask may answer it, and a stale card or
another owner's listing cannot. An owner nobody polls (``GET /api/approvals?owner=…``) for
the broker's grace window has its asks denied as "stopped watching".

Chat asks share the broker but are not listed: the Chat page shows and answers them.
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

_OPERATION = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z")


def _first_line(text, limit: int = 200):
    if not isinstance(text, str):
        return None
    line = text.strip().splitlines()[0] if text.strip() else ""
    return line[:limit]


def mark_covered(ctx, live: list[dict], file_rows: list[dict] | None = None) -> list[dict]:
    """Name, on each live ask, the session-file request that is the same ask.

    An owned flow step's worker also publishes its ask in its own session's file channel
    (``read_model.inbox`` lists it), while the step waits on the dashboard broker. The
    two are one approval: ``covers`` lists the file requests (``session_id``,
    ``approval_id``) of this flow's step sessions with the same action, so a page shows
    and counts it once, answered through the broker the step waits on. The read-model
    list itself is left as it is.
    """
    if not live:
        return live
    from garuda.context.redact import redact_text
    from garuda.core import read_model

    flows = {row["session_id"] for row in live if row.get("session_id")}
    if file_rows is None:
        file_rows = []
        try:
            metas = ctx.store.list_sessions(limit=None, unreadable=[])
        except Exception:  # noqa: BLE001 - nothing marked covered, never a guess
            metas = []
        for meta in metas:
            step = meta.get("flow_step") if isinstance(meta.get("flow_step"), dict) else {}
            if step.get("flow_session") in flows:
                try:
                    file_rows += [{"session_id": meta["session_id"], **row}
                                  for row in read_model.approvals(ctx.store, meta["session_id"])]
                except Exception:  # noqa: BLE001
                    continue
    parents: dict[str, str | None] = {}

    def parent(session_id):
        if session_id not in parents:
            try:
                step = ctx.store.load_meta(session_id).get("flow_step")
                parents[session_id] = step.get("flow_session") if isinstance(step, dict) else None
            except Exception:  # noqa: BLE001
                parents[session_id] = None
        return parents[session_id]

    for row in live:
        action = redact_text(str(row.get("action", "")))[0]
        row["covers"] = [{"session_id": f["session_id"], "approval_id": f.get("approval_id")}
                         for f in file_rows
                         if f.get("action") == action and parent(f["session_id"]) == row.get("session_id")]
    return live


def _pending(broker) -> list[dict]:
    """The broker's pending asks; read from an HTTP thread while the loop may park one."""
    for _ in range(3):
        try:
            return broker.pending()
        except RuntimeError:  # the dict changed size mid-iteration: read it again
            continue
    return []


def live_approvals(ctx) -> list[dict]:
    """Every waiting broker ask of a dashboard operation in an allowlisted workspace."""
    if ctx.live is None or ctx.flow_starts is None:
        return []
    allowed = {str(path) for path in ctx.allowed_workspaces}
    rows = []
    for ask in _pending(ctx.live.approvals):
        owner = ask.get("owner")
        if not isinstance(owner, str) or not _OPERATION.match(owner):
            continue  # a chat's ask: the Chat page owns it
        try:
            record = ctx.flow_starts.intents.read(owner)
        except Exception:  # noqa: BLE001 - an unreadable record lists nothing, never a guess
            logger.warning("Operation %s could not be read for its approvals", owner, exc_info=True)
            continue
        if record is None or record.get("workspace") not in allowed:
            continue
        session_id = record.get("session_id") or record.get("reserved_session")
        name = task = None
        if isinstance(session_id, str):
            try:
                meta = ctx.store.load_meta(session_id)
                # The user's own task field when the starter recorded it; the compiled
                # task opens with the starter's brief, which says nothing about this run.
                starter = meta.get("starter") if isinstance(meta.get("starter"), dict) else {}
                inputs = starter.get("inputs") if isinstance(starter.get("inputs"), dict) else {}
                recorded = next((inputs[key] for key in ("goal", "question", "feedback")
                                 if isinstance(inputs.get(key), str)), None)
                name, task = meta.get("name"), _first_line(recorded or meta.get("task"))
                flow = meta.get("flow") if isinstance(meta.get("flow"), dict) else {}
                name = name or flow.get("name")
            except Exception:  # noqa: BLE001 - the ask is still listed; its label is unknown
                pass
        rows.append({**ask, "source": "dashboard-operation", "operation_id": owner,
                     "session_id": session_id, "workspace": record.get("workspace"),
                     "name": name, "task": task,
                     "timeout_seconds": ctx.live.approvals.timeout_seconds,
                     "grace_seconds": ctx.live.approvals.grace_seconds})
    return rows
