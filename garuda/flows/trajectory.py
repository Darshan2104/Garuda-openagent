"""A flow run's whole trajectory, for study and export (#726).

``garuda flow show FLOW --json`` prints :func:`trajectory`; ``garuda flow export
FLOW --out FILE`` writes :func:`export_records` as JSONL. Both read only: the
flow's journal and receipts, each step session's record, its persisted ACP
event trail, its usage-ledger rows and the flow's check records (read without
their store lock). Nothing here starts, locks or repairs.

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

_SESSION_FIELDS = ("runtime", "model", "reported_model", "agent", "agent_digest", "agent_segment", "status",
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


def steps_reported_usage(view: dict, rows: list[dict] | None = None) -> dict:
    """A flow's step sessions' adapter-reported usage, totalled (#755).

    ``view`` is :func:`garuda.core.read_model.flow`. Each step session (and
    review-group member) counts once, its latest report per segment as
    :func:`~garuda.observability.usage.reported_usage` reads it. The harnesses'
    own figures, labelled as such: never added to Garuda's metered usage.
    """
    from garuda.observability.usage import reported_usage

    ids: list[str] = []
    for step in view.get("steps") or []:
        for attempt in step.get("attempts") or []:
            members = attempt.get("members") or []
            for sid in [attempt.get("session_id"), *(m.get("session_id") for m in members
                                                     if isinstance(m, dict))]:
                if isinstance(sid, str) and sid and sid not in ids:
                    ids.append(sid)
    rows = _ledger_rows() if rows is None else rows
    out: dict[str, Any] = {
        "source": "adapter_reported",
        "note": "the step sessions' adapter reports, totalled; not metered or priced by Garuda",
        "step_sessions": len(ids)}
    if rows is None:
        return {**out, "coverage": "ledger_unreadable", "reporting_sessions": None,
                "cost_usd": None, "cost_complete": False, "models": []}
    reports = [report for sid in ids if (report := reported_usage(sid, records=rows))]
    costs = [r["cost_usd"] for r in reports if isinstance(r.get("cost_usd"), (int, float))]
    return {**out, "coverage": "complete", "reporting_sessions": len(reports),
            "cost_usd": round(sum(costs), 8) if costs else None,
            "cost_complete": bool(costs) and all(r.get("cost_complete") for r in reports),
            "models": sorted({m for r in reports for m in r.get("models") or []})}


def steps_reported_usage_line(total: dict | None) -> str:
    """One human line for :func:`steps_reported_usage`."""
    if not total:
        return "not reported"
    if total.get("coverage") != "complete":
        return "unknown (usage ledger unreadable)"
    if not total["reporting_sessions"]:
        return f"not reported by any of {total['step_sessions']} step sessions"
    cost = total["cost_usd"]
    parts = [f"USD {cost}" if cost is not None else "cost not reported",
             f"{total['reporting_sessions']} of {total['step_sessions']} step sessions reporting"]
    if total["models"]:
        parts.append("model " + ", ".join(total["models"]))
    return "; ".join(parts) + " — adapter-reported, not metered by Garuda"


def _events(store, session_id: str) -> tuple[list[dict], dict]:
    """The session's persisted ACP event trail, bounded, as it was written.

    The trail may be size-rotated into several files (``event_log``); they are
    read in order under one byte bound, oldest first.
    """
    from garuda.observability import event_log

    directory = Path(store.session_dir(session_id))
    paths = event_log.part_paths(directory)
    if not paths:
        return [], {"source": None, "count": 0, "by_kind": {}, "omitted": 0, "unreadable": 0}
    rows, by_kind, omitted, unreadable = [], {}, 0, 0
    lines, budget, truncated = [], MAX_EVENT_BYTES, False
    for path in paths:
        if budget <= 0:
            truncated = True
            break
        with path.open("rb") as handle:
            data = handle.read(budget + 1)
        cut = len(data) > budget
        part = data[:budget].splitlines()
        if cut and part:
            part.pop()  # a line cut at the byte bound is not a record
        lines.extend(part)
        budget -= min(len(data), budget)
        if cut:
            truncated = True
            break
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
    facts = event_log.trail_facts(directory)
    return rows, {"source": "acp-events.jsonl", "count": len(rows) + omitted,
                  "by_kind": by_kind, "omitted": omitted, "unreadable": unreadable,
                  "truncated": truncated,
                  **({"parts": facts["parts"], "pruned": facts["pruned"]}
                     if facts["parts"] > 1 or facts["pruned"] else {})}


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


def _what_ran(attempt: dict, session: dict | None) -> None:
    """Fill what an attempt's receipt does not record from its step session (#752).

    A receipt's role plan records a configured ``model_id`` but not the model a
    harness chose itself, nor the adapter version. ``model_source`` labels where
    ``model`` came from: ``role`` (configured), ``agent`` (the ACP agent's reported
    current model) or ``session`` (a native session's model); ``None`` when unknown.
    """
    session = session if isinstance(session, dict) and session.get("readable") else {}
    if not attempt.get("model"):
        model, source = session.get("reported_model"), "agent"
        if not model and session.get("runtime") == "native":
            model, source = session.get("model"), "session"
        attempt["model"], attempt["model_source"] = (model, source) if model else (None, None)
    if attempt.get("adapter_version") is None:
        role = session.get("role") if isinstance(session.get("role"), dict) else {}
        adapter = role.get("adapter") if isinstance(role.get("adapter"), dict) else {}
        segment = session.get("agent_segment") if isinstance(session.get("agent_segment"), dict) else {}
        attempt["adapter_version"] = adapter.get("version") or segment.get("reported_version")


def verification(store, flow_session: str, meta: dict) -> dict:
    """The flow's verification verdict and each check's record, read only (#752)."""
    from garuda.flows.verification_records import study
    from garuda.runtime.session_state import effective_state

    return study(store.session_dir(flow_session), flow_session, meta,
                 effective_state(meta).get("verification"))


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
            _what_ran(record, record["session"])
            if with_events:
                record["events_records"] = events
            for member in record["members"]:
                member["session"], member_events = _session(store, member.get("session_id"), rows)
                _what_ran(member, member["session"])
                if with_events:
                    member["events_records"] = member_events
            attempts.append(record)
    attempts.sort(key=lambda a: launched.get((a["step"], a["attempt"]), len(journal)))
    durations = [a["duration_ms"] for a in attempts if type(a.get("duration_ms")) is int]
    checked = verification(store, flow_session, meta)
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
        # Own field beside the review: whether the flow's result passed its checks.
        "verification": checked,
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
            "verification": checked["status"],
            "checks": len(checked["checks"]),
        },
    }


def export_records(store, flow_session: str, meta: dict | None = None) -> Iterator[dict]:
    """The trajectory as ordered JSONL records: ``flow``, ``journal``, then each
    ``attempt`` followed by its session's ``event`` records, then one ``check``
    record per flow check, then ``summary``."""
    full = trajectory(store, flow_session, meta, with_events=True)
    exported_at = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
    head = {key: value for key, value in full.items()
            if key not in ("journal", "attempts", "summary")}
    # The verdict stays on the flow record; its checks get their own records.
    head["verification"] = {key: value for key, value in full["verification"].items()
                            if key != "checks"}
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
    for check in full["verification"]["checks"]:
        yield {"record": "check", "flow_session": flow_session, **check}
    yield {"record": "summary", "flow_session": flow_session, **full["summary"]}


__all__ = ["SCHEMA", "VERSION", "export_records", "steps_reported_usage",
           "steps_reported_usage_line", "trajectory", "verification"]
