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

**Consults (G.4).** A consulted role runs as its own session, so a conversation also lists the
consults requested under it (and under its flow's step sessions) and rolls their usage up: the
descendants' ledger records are added to the parent's **once** (records are de-duplicated by
their ledger key), with ``origin`` (``run``, ``consult`` …) kept as a grouping dimension beside
the original ``call_purpose``, so a consulted summarizer call stays a summarizer call. Global
statistics sum the original records and never the parent rollups, so nothing is counted twice.
"""

from __future__ import annotations

from typing import Any

from garuda.consult.view import summary as consult_summary
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


def _prompt_digest(value) -> bool:
    return (isinstance(value, str) and len(value) == 64
            and all(char in "0123456789abcdef" for char in value))


def _distinct_prompts(prompts: list[dict]) -> list[dict]:
    """Newest measurements first; deduplication is scoped by the caller."""
    seen, distinct = set(), []
    for prompt in reversed(prompts):
        if prompt["digest"] not in seen:
            seen.add(prompt["digest"])
            distinct.append(prompt)
    return distinct[:5]


def agent_info(store, session_id: str, meta: dict) -> dict:
    """Recorded native execution bindings and actual outbound measurements.

    Historical records without bindings remain explicitly unattributed. Current
    session metadata describes the session, never a historical prompt's owner.
    Compatibility aggregate fields still cover the complete session.
    """
    import json

    prompts: list[dict] = []
    unattributed: list[dict] = []
    segments: dict[tuple, dict] = {}
    try:
        with store.events_path(session_id).open(encoding="utf-8") as handle:
            for line in handle:
                if '"system_prompt"' not in line:
                    continue
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                payload = event.get("payload") or {}
                if event.get("type") != "system_prompt" or not payload.get("digest"):
                    continue
                prompt = {"digest": payload["digest"], "chars": payload.get("chars")}
                prompts.append(prompt)
                binding = payload.get("agent_segment")
                if (not isinstance(binding, dict) or not isinstance(binding.get("id"), str)
                        or not binding["id"] or binding.get("runtime") != "native"
                        or binding.get("kind") != "native_execution"
                        or not isinstance(binding.get("name"), str) or not binding["name"]
                        or (binding.get("digest") is not None
                            and not _prompt_digest(binding["digest"]))):
                    unattributed.append(prompt)
                    continue
                key = (binding["id"], binding["name"], binding.get("digest"))
                segment = segments.setdefault(key, {
                    "id": binding["id"], "name": binding["name"], "digest": binding.get("digest"),
                    "runtime": "native", "kind": "native_execution", "prompts": [],
                })
                segment["prompts"].append(prompt)
    except OSError:
        pass
    recorded = []
    for segment in reversed(list(segments.values())):
        recorded.append({**segment, "prompt_changes": len(segment["prompts"]),
                         "prompts": _distinct_prompts(segment["prompts"])})
    role = meta.get("role") if isinstance(meta.get("role"), dict) else {}
    return {"name": meta.get("agent"), "digest": meta.get("agent_digest"),
            "role_agent": role.get("agent"), "segment": meta.get("agent_segment"),
            "prompts": _distinct_prompts(prompts), "prompt_changes": len(prompts),
            "segments": recorded[:20], "segment_count": len(recorded),
            "unattributed": _distinct_prompts(unattributed),
            "unattributed_changes": len(unattributed)}


def consult_rows(store, session_id: str, meta: dict) -> list[dict]:
    """The consults requested under this session and its flow's step sessions."""
    from garuda.consult import view

    roots = [session_id] + [sid for sid, _ in _flow_sessions(store, session_id, meta)
                            if sid != session_id]
    rows: list[dict] = []
    for root in dict.fromkeys(roots):
        rows.extend(view.entries(store, root))
    return rows


def models_used(store, session_id: str, meta: dict, ledger, consults=()) -> dict:
    flow_sessions = _flow_sessions(store, session_id, meta)
    ids = {session_id} | {sid for sid, _ in flow_sessions} | {
        row["child_session"] for row in consults if row.get("child_session")}
    flow_roles = {sid: role for sid, role in flow_sessions}
    selected = (meta.get("role") or {}).get("model_id") if isinstance(meta.get("role"), dict) \
        else None
    selected = selected or meta.get("model")
    records, seen = [], set()
    for record in (ledger.records() if ledger else []):
        if record.get("session_id") not in ids:
            continue
        key = record.get("key")
        if key is not None:
            if key in seen:
                continue  # a descendant's event is rolled up once
            seen.add(key)
        records.append(record)
    rows: dict[tuple, dict] = {}
    current = {"origin": "run"}

    def row_for(work_type, harness, model, selected_model):
        key = (current["origin"], work_type, harness, model)
        row = rows.setdefault(key, _row(work_type, harness, model, selected_model))
        row["origin"] = current["origin"]
        return row

    snapshots = []
    for record in records:
        kind = record.get("kind")
        current["origin"] = record.get("origin") or "run"
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
    origins = sorted({r.get("origin") or "run" for r in records})
    return {"source": source, "label": label,
            "rows": sorted(rows.values(), key=lambda r: (r.get("origin", "run") != "run",
                                                         r["work_type"], str(r["model"]))),
            # Own work and each descendant origin, which add up to ``totals`` exactly.
            "by_origin": {o: ledger_module.totals([r for r in records
                                                   if (r.get("origin") or "run") == o])
                          for o in origins},
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
    consults = consult_rows(store, session_id, meta)
    return {
        "session": {k: row[k] for k in ("session_id", "name", "kind", "label", "state", "task",
                                        "runtime", "model", "verification")},
        "selected": {"harness": role.get("runtime_id"), "model_id": role.get("model_id")
                     or meta.get("model"), "fallback": role.get("fallback")},
        "lanes": lanes,
        "agent": agent_info(store, session_id, meta),
        "consults": {"summary": consult_summary(consults), "entries": consults},
        "models_used": models_used(store, session_id, meta, ledger, consults),
        "links": _story(store, session_id, meta),
    }
