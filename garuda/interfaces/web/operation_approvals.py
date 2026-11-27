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
