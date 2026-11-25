"""A flow run's whole trajectory, for study and export (#726).

``garuda flow show FLOW --json`` prints :func:`trajectory`; ``garuda flow export
FLOW --out FILE`` writes :func:`export_records` as JSONL. Both read only: the
flow's journal and receipts, each step session's record, its persisted ACP
event trail and its usage-ledger rows. Nothing here starts, locks or repairs.

What is exported is what Garuda already persisted, at the level the user chose
(``observability.trace_detail``). Native step transcripts (messages and tool
output) are never exported; a native step contributes its record and usage only.
Free text a session recorded (task, stop and error messages) is redacted again
on the way out.
"""

from __future__ import annotations

import datetime as _dt
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

SCHEMA = "garuda.flow-trajectory"
VERSION = 1
#: Persisted ACP events exported per step session; the rest are counted.
MAX_EVENTS = 5000
#: Bytes of one session's ACP event trail read at most.
MAX_EVENT_BYTES = 16 * 1024 * 1024

_SESSION_FIELDS = ("runtime", "model", "agent", "agent_digest", "agent_segment", "status",
                   "state", "label", "verification", "isolation", "created_at", "updated_at")


def _redacted(value: Any, limit: int = 2000) -> Any:
    if not isinstance(value, str):
        return value
    from garuda.context.redact import redact_text

    return redact_text(value)[0][:limit]


def _ledger_rows() -> list[dict] | None:
    try:
        from garuda.observability.ledger import Ledger

        # A list: ``records()`` is a generator, and every attempt reads these rows
        # twice (metered and reported); a generator would serve only the first (#748).
        return list(Ledger().records())
    except Exception:
        return None


def _usage(session_id: str, rows: list[dict] | None) -> dict:
    """Garuda's metered usage and the adapter's own report, never added together."""
    from garuda.observability.ledger import totals
    from garuda.observability.usage import reported_usage

    if rows is None:
        return {"metered": None, "reported": None, "coverage": "ledger_unreadable"}
    own = [row for row in rows if row.get("session_id") == session_id]
    return {"metered": totals(own) if own else None,
            "reported": reported_usage(session_id, records=rows)}


def _events(store, session_id: str) -> tuple[list[dict], dict]:
    """The session's persisted ACP event trail, bounded, as it was written."""
    path = Path(store.session_dir(session_id)) / "acp-events.jsonl"
    if not path.is_file():
        return [], {"source": None, "count": 0, "by_kind": {}, "omitted": 0, "unreadable": 0}
    rows, by_kind, omitted, unreadable = [], {}, 0, 0
    with path.open("rb") as handle:
        data = handle.read(MAX_EVENT_BYTES + 1)
    truncated = len(data) > MAX_EVENT_BYTES
    lines = data[:MAX_EVENT_BYTES].splitlines()
    if truncated and lines:
        lines.pop()  # a line cut at the byte bound is not a record
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            unreadable += 1
            continue
        if not isinstance(row, dict):
            unreadable += 1
            continue
        kind = str(row.get("kind"))
        by_kind[kind] = by_kind.get(kind, 0) + 1
        if len(rows) < MAX_EVENTS:
            rows.append(row)
        else:
            omitted += 1
    return rows, {"source": "acp-events.jsonl", "count": len(rows) + omitted,
                  "by_kind": by_kind, "omitted": omitted, "unreadable": unreadable,
                  "truncated": truncated}


def _session(store, session_id: str | None, rows: list[dict] | None) -> tuple[dict | None, list]:
    if not session_id:
        return None, []
    from garuda.core import read_model
    from garuda.core.approval_outcome import approval_summary

    try:
        meta = store.load_meta(session_id)
    except Exception as exc:
        return {"session_id": session_id, "readable": False,
                "reason": type(exc).__name__}, []
    row = read_model.session_row(store, meta)
    out: dict[str, Any] = {"session_id": session_id, "readable": True,
                           **{key: row.get(key) for key in _SESSION_FIELDS},
                           "workspace_kind": meta.get("workspace_kind"),
                           "trace_detail": read_model.trace_detail(meta)}
    role = meta.get("role")
    out["role"] = role if isinstance(role, dict) else None
    out["initial_selection"] = meta.get("initial_selection")
    acp_session = meta.get("acp_session")
    out["acp_session"] = acp_session if isinstance(acp_session, dict) else None
    out["error"] = ({"code": meta.get("error_code"), "detail": _redacted(meta.get("error_detail"), 500)}
                    if meta.get("error_code") else None)
    out["stop_reason"] = meta.get("stop_reason")
    out["approval_summary"] = approval_summary(meta)
    out["usage"] = _usage(session_id, rows)
    try:
        events, summary = _events(store, session_id)
    except OSError as exc:
        events, summary = [], {"source": "acp-events.jsonl", "error": type(exc).__name__}
    out["events"] = summary
    return out, events


def trajectory(store, flow_session: str, meta: dict | None = None, *,
               with_events: bool = False) -> dict:
    """Every step attempt of a flow, in order, with what ran, when and at what cost.

    With ``with_events`` each attempt's session carries its persisted ACP events
    under ``events_records`` (``export_records`` writes them as their own lines).
    """
    from garuda.core import read_model
    from garuda.flows import engine

    meta = meta if meta is not None else store.load_meta(flow_session)
    view = read_model.flow(store, flow_session, meta)
    if view is None:
        raise ValueError(f"session {flow_session} is not a flow session")
    rows = _ledger_rows()
    journal = [{key: entry.get(key) for key in ("event", "index", "step", "attempt", "at")
                if key in entry} for entry in engine.journal(store, flow_session)]
    # The journal's intents are the order attempts launched in: a review loop
    # alternates steps, which receipt order (step index, attempt) does not show.
    launched = {}
    for position, entry in enumerate(journal):
        if entry.get("event") == "intent":
            launched.setdefault((entry.get("step"), entry.get("attempt")), position)
    attempts = []
    for step in view["steps"]:
        for attempt in step["attempts"]:
            record = {"step": step["id"], **attempt}
            record["session"], events = _session(store, attempt["session_id"], rows)
            if with_events:
                record["events_records"] = events
            for member in record["members"]:
                member["session"], member_events = _session(store, member.get("session_id"), rows)
                if with_events:
                    member["events_records"] = member_events
            attempts.append(record)
    attempts.sort(key=lambda a: launched.get((a["step"], a["attempt"]), len(journal)))
    durations = [a["duration_ms"] for a in attempts if type(a.get("duration_ms")) is int]
    costs = [cost for a in attempts
             for usage in [((a.get("session") or {}).get("usage") or {})]
             for cost in [(usage.get("reported") or {}).get("cost_usd")]
             if isinstance(cost, (int, float))]
    return {
        "schema": SCHEMA, "version": VERSION, "flow_session": flow_session,
        "name": view["name"], "state": view["state"],
        "task": _redacted(meta.get("task")),
        "workspace": meta.get("workspace"),
        "created_at": meta.get("created_at"), "updated_at": meta.get("updated_at"),
        "declared_steps": [step["id"] for step in view["steps"]],
        "stop": view["stop"], "review": view["review"],
        "quota_recovery": view["quota_recovery"], "coverage": view["coverage"],
        "edges": view["edges"],
        "journal": journal,
        "attempts": attempts,
        "summary": {
            "attempts": len(attempts),
            "steps": len(view["steps"]),
            "review_rounds": (view["review"] or {}).get("rounds")
            if isinstance(view["review"], dict) else None,
            "timed_attempts": len(durations),
            "duration_ms": sum(durations) if durations else None,
            # The harnesses' own figures, labelled as such; never Garuda's accounting.
            "reported_cost_usd": round(sum(costs), 8) if costs else None,
        },
    }


def export_records(store, flow_session: str, meta: dict | None = None) -> Iterator[dict]:
    """The trajectory as ordered JSONL records: ``flow``, ``journal``, then each
    ``attempt`` followed by its session's ``event`` records, then ``summary``."""
    full = trajectory(store, flow_session, meta, with_events=True)
    exported_at = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
    head = {key: value for key, value in full.items()
            if key not in ("journal", "attempts", "summary")}
    yield {"record": "flow", **head, "exported_at": exported_at}
    for entry in full["journal"]:
        yield {"record": "journal", **entry}
    for attempt in full["attempts"]:
        events = attempt.pop("events_records", [])
        member_events = [(member, member.pop("events_records", [])) for member in attempt["members"]]
        yield {"record": "attempt", **attempt}
        where = {"step": attempt["step"], "attempt": attempt["attempt"]}
        for event in events:
            yield {"record": "event", **where, "session_id": attempt["session_id"], "event": event}
        for member, rows in member_events:
            for event in rows:
                yield {"record": "event", **where, "member": member.get("role"),
                       "session_id": member.get("session_id"), "event": event}
    yield {"record": "summary", "flow_session": flow_session, **full["summary"]}


__all__ = ["SCHEMA", "VERSION", "export_records", "trajectory"]
