"""The conversation read model: who did what, on which models (plan task F.1, #169).

One conversation is a session plus the sessions linked to it: those it resumed, those it
tagged for context, those that tagged it, and — for a flow — its step sessions. It reads
persisted records only (session metadata, the usage ledger, the trace lanes) and starts
nothing.

**Models used.** Grouped by *work type* × harness × model from the **usage ledger**.
Work type is the original ``call_purpose`` of a native call (``controller``, ``collector``,
``classifier``, ``summarizer`` …) or, for the sessions of a flow, the flow step's role. A
native session with no ledger records (one that predates it) falls back to its own
``aggregate_model_metrics`` and says so ("from session metrics").

Three separations are kept on purpose:

* the **selected** model (what the role asked for) is not the **reported** one: an
  external harness's internal calls on other models read ``not reported``, and unattributed
  usage is never assigned to the selected model;
* **context snapshots** (occupancy, latest report) are not billable usage;
* **ACP turns** are not native call counts.
"""

from __future__ import annotations

from typing import Any

from garuda.core import read_model
from garuda.observability import ledger as ledger_module

NOT_REPORTED = "not reported"
FROM_METRICS = "from session metrics"
MAX_CHAIN = 12


def _story(store, session_id: str, meta: dict) -> dict:
    """The sessions this one is linked to, each once."""
    chain, seen = [], {session_id}
    current = meta
    while len(chain) < MAX_CHAIN and current.get("resumed_from"):
        previous = current["resumed_from"]
        if previous in seen:
            break
        seen.add(previous)
        try:
            current = store.load_meta(previous)
        except (OSError, ValueError):
            chain.append({"session_id": previous, "unavailable": True})
            break
        chain.append(_ref(previous, current))
    resumed_into = []
    for other in store.list_sessions(limit=None):
        if other.get("resumed_from") == session_id:
            resumed_into.append(_ref(other["session_id"], other))
    tagged = [{**_ref(link["session_id"], {"name": link.get("name")}),
               "provenance": link.get("provenance"), "cross_project": bool(link.get("cross_project")),
               "receipt": _receipt(store, session_id, link) if link.get("cross_project") else None}
              for link in meta.get("context_from") or []]
    tagged_by = [_ref(link["session_id"], {}) for link in meta.get("context_to") or []]
    return {"resumed_from": chain, "resumed_into": resumed_into, "tagged": tagged,
            "tagged_by": tagged_by}


def _receipt(store, session_id: str, link: dict) -> dict | None:
    """The cross-project sharing receipt written when a grant let this session read another
    project's session: its fingerprint and time, never the shared content."""
    import json

    path = store.session_dir(session_id) / "receipts" / f"{link['session_id']}.json"
    try:
        receipt = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"present": False}
    return {"present": True, "fingerprint": receipt.get("fingerprint"),
            "created_at": receipt.get("created_at"), "fields": receipt.get("fields")}


def _ref(session_id: str, meta: dict) -> dict:
    return {"session_id": session_id, "name": meta.get("name"), "task": meta.get("task"),
            "label": read_model.session_row(None, {**meta, "session_id": session_id})["label"]
            if meta.get("state") or meta.get("status") else None}


def _flow_sessions(store, session_id: str, meta: dict) -> list[tuple[str, str | None]]:
    """``[(step session id, role)]`` for a flow's receipts."""
    if meta.get("kind") != "flow":
        return []
    from garuda.flows import engine

    out = []
    for receipt in engine.receipts(store, session_id):
        sid = receipt.get("session_id")
        if sid:
            out.append((sid, receipt.get("role") or receipt.get("step")))
        for member in receipt.get("members") or []:
            if member.get("session_id"):
                out.append((member["session_id"], member.get("role")))
    return out


def _work_type(record: dict, flow_roles: dict[str, str | None]) -> str:
    role = flow_roles.get(record.get("session_id"))
    if role:
        return f"flow step: {role}"
    return record.get("call_purpose") or "unknown"


def _row(work_type, harness, model, selected) -> dict:
    return {"work_type": work_type, "harness": harness, "model": model, "selected_model": selected,
            "calls": 0, "turns": 0, "input_tokens": 0, "output_tokens": 0, "total_tokens": 0,
            "cost_usd": 0.0, "cost_unknown": 0, "snapshots": 0}


def models_used(store, session_id: str, meta: dict, ledger) -> dict:
    flow_sessions = _flow_sessions(store, session_id, meta)
    ids = {session_id} | {sid for sid, _ in flow_sessions}
    flow_roles = {sid: role for sid, role in flow_sessions}
    selected = (meta.get("role") or {}).get("model_id") if isinstance(meta.get("role"), dict) \
        else None
    selected = selected or meta.get("model")
    records = [r for r in ledger.records() if r.get("session_id") in ids] if ledger else []
    rows: dict[tuple, dict] = {}

    def row_for(work_type, harness, model, selected_model):
        key = (work_type, harness, model)
        return rows.setdefault(key, _row(work_type, harness, model, selected_model))

    snapshots = []
    for record in records:
        kind = record.get("kind")
        harness = record.get("harness") or ("native" if kind == "native_model_call" else None)
        if kind == "native_model_call":
            row = row_for(_work_type(record, flow_roles), harness or "native",
                          record.get("model") or NOT_REPORTED, selected)
            row["calls"] += record.get("calls", 1)
        elif kind == "acp_usage_delta":
            row = row_for(_work_type(record, flow_roles) if flow_roles else "turn usage",
                          harness or record.get("adapter"),
                          record.get("model") or NOT_REPORTED, selected)
            row["turns"] += 1  # an ACP turn, never a native call
        elif kind == "acp_usage_snapshot":
            snapshots.append({k: record.get(k) for k in (
                "harness", "model", "context_used", "context_size", "input_tokens",
                "output_tokens", "cumulative", "delta_unavailable", "time")})
            continue
        else:
            continue
        row["input_tokens"] += record.get("input_tokens") or 0
        row["output_tokens"] += record.get("output_tokens") or 0
        row["total_tokens"] += (record["total_tokens"] if record.get("total_tokens") is not None
                                else (record.get("input_tokens") or 0)
                                + (record.get("output_tokens") or 0))
        if record.get("cost_usd") is None:
            row["cost_unknown"] += 1
        else:
            row["cost_usd"] = round(row["cost_usd"] + record["cost_usd"], 8)
    source, label = "ledger", None
    if not rows and not snapshots:
        fallback = _from_metrics(store, session_id, meta, selected)
        if fallback:
            rows, source, label = fallback, "session_metrics", FROM_METRICS
    return {"source": source, "label": label,
            "rows": sorted(rows.values(), key=lambda r: (r["work_type"], str(r["model"]))),
            # Occupancy and cumulative reports are display evidence, never billable usage.
            "snapshots": snapshots[-5:],
            "totals": ledger_module.totals(records) if records else None}


def _from_metrics(store, session_id: str, meta: dict, selected) -> dict:
    import json

    from garuda.core.metrics import aggregate_model_metrics

    path = store.events_path(session_id)
    try:
        events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
                  if line.strip()]
    except (OSError, ValueError):
        return {}
    rows = {}
    for purpose, bucket in aggregate_model_metrics(events)["by_call_purpose"].items():
        row = _row(purpose, "native", meta.get("model") or NOT_REPORTED, selected)
        row.update(calls=bucket["calls"], input_tokens=bucket["prompt_tokens"],
                   output_tokens=bucket["completion_tokens"], total_tokens=bucket["total_tokens"],
                   cost_usd=bucket["cost_usd"], cost_unknown=bucket["cost_unknown_calls"])
        rows[(purpose, "native", row["model"])] = row
    return rows


def conversation(store, session_id: str, *, ledger=None, queue=None) -> dict[str, Any]:
    """Everything the conversation view shows for one session."""
    from garuda.observability.lanes import read_cross_runtime

    meta = store.load_meta(session_id)
    row = read_model.session(store, session_id, queue=queue)
    trace = read_cross_runtime(store.session_dir(session_id))
    lanes = [{"runtime_id": lane.runtime_id, "kind": lane.kind, "version": lane.version,
              "native_session_id": lane.native_session_id,
              "native_events": (None if lane.native_event_start is None
                                else lane.native_event_end - lane.native_event_start),
              "external_events": lane.external_events} for lane in trace.lanes]
    role = meta.get("role") if isinstance(meta.get("role"), dict) else {}
    return {
        "session": {k: row[k] for k in ("session_id", "name", "kind", "label", "state", "task",
                                        "runtime", "model", "verification")},
        "selected": {"harness": role.get("runtime_id"), "model_id": role.get("model_id")
                     or meta.get("model"), "fallback": role.get("fallback")},
        "lanes": lanes,
        "models_used": models_used(store, session_id, meta, ledger),
        "links": _story(store, session_id, meta),
    }
