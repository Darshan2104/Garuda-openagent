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
accounting); the row says ``unknown``. Persisted run-tree gate spend is projected
separately, with explicit uncertainty and without rebuilding accounting.
"""

from __future__ import annotations

import math
import time
from pathlib import Path
from typing import Any

from garuda.core.spend import SPEND_OPTIONAL_COUNTERS, SPEND_REQUIRED_FIELDS
from garuda.runtime.session_state import ACTIVE_WORK, effective_state, is_crashed, summary_label

UNKNOWN = "unknown"


def _queue_entries(queue) -> list[dict] | None:
    """The queue's entries, or ``None`` when it cannot be read."""
    try:
        return queue.entries()
    except Exception:
        return None


def _queue_index(queue) -> dict[str, dict]:
    """``{session_id: {state, position, scope}}``; ``{"": {...unknown}}`` marks an unreadable queue."""
    return {} if queue is None else _index_entries(_queue_entries(queue))


def _index_entries(entries: list[dict] | None) -> dict[str, dict]:
    if entries is None:
        return {"": {"state": UNKNOWN}}
    return {e["id"]: {"state": e["state"], "position": e["position"], "scope": e["scope"],
                      "harness": e.get("harness"), "quarantined": e.get("quarantined", False)}
            for e in entries}


def approvals(store, session_id: str, *, requests: list[dict] | None = None) -> list[dict]:
    """Pending approval requests of one session (answered ones are not listed)."""
    from garuda.acp.approval_channel import FileApprovalChannel

    try:
        channel = FileApprovalChannel(Path(store.session_dir(session_id)) / "approvals",
                                      session_id)
        from garuda.context.redact import redact_text

        out = []
        for r in channel.pending() if requests is None else requests:
            out.append({"approval_id": r.get("approval_id"),
                        "action": redact_text(str(r.get("action", "")))[0],
                        **{k: r.get(k) for k in ("family", "runtime_id", "expires_at", "ceiling",
                                                 "digest")},
                        **({"requester": {k: redact_text(v)[0] if isinstance(v, str) else v
                                          for k, v in r["requester"].items()}}
                           if isinstance(r.get("requester"), dict) else {}),
                        "expired": time.time() > float(r.get("expires_at") or 0)})
        return out
    except Exception:
        return []


def _ref(ref: dict) -> dict:
    return {k: ref.get(k) for k in ("type", "digest", "producer_step", "producer_session",
                                    "attempt", "size")}


def _ran(role_plan) -> dict:
    """The runtime identity a step attempt's receipt recorded for what ran (#726).

    ``runtime`` is the harness that ran it, ``fallback`` the start-time decision
    when the role has a chain. Receipts written before the plan was recorded, and
    attempts refused before a role resolved, report ``None``. ``model_source`` is
    ``role`` for a configured ``model_id``; a role that leaves the model to the
    harness reports ``None`` here, and the trajectory fills it from the step
    session (#752).
    """
    plan = role_plan if isinstance(role_plan, dict) else {}
    agent = plan.get("agent") if isinstance(plan.get("agent"), dict) else {}
    adapter = plan.get("adapter") if isinstance(plan.get("adapter"), dict) else {}
    fallback = plan.get("fallback") if isinstance(plan.get("fallback"), dict) else None
    return {
        "runtime": plan.get("runtime_id"), "runtime_kind": plan.get("kind"),
        "model": plan.get("model_id"), "model_source": "role" if plan.get("model_id") else None,
        "effort": plan.get("effort"),
        "permissions": plan.get("permissions"), "harness_mode": plan.get("harness_mode"),
        "role_digest": plan.get("digest"),
        "agent": {"name": agent.get("name"), "digest": agent.get("digest")} if agent else None,
        "adapter_version": adapter.get("version"),
        "fallback": {k: fallback.get(k) for k in ("primary", "taken", "skipped")}
        if fallback else None,
    }


def reported_model(meta: dict) -> str | None:
    """The model an ACP agent reported running a session with (#752), or ``None``.

    A model Garuda set is the one the agent confirmed selecting (the applied
    ``acp_options``); otherwise the ``model`` option's current value the agent
    offered at ``session/new`` (``acp_session``), i.e. its own choice.
    """
    if not isinstance(meta, dict):
        return None
    role = meta.get("role") if isinstance(meta.get("role"), dict) else {}
    for applied in (role.get("acp_options"), meta.get("acp_options")):
        if isinstance(applied, dict) and isinstance(applied.get("model"), str) and applied["model"]:
            return applied["model"][:200]
    acp = meta.get("acp_session")
    options = acp.get("config_options") if isinstance(acp, dict) else None
    for option in options if isinstance(options, list) else []:
        if isinstance(option, dict) and option.get("id") == "model":
            current = option.get("current")
            return current[:200] if isinstance(current, str) and current else None
    return None


def _timings(receipt: dict) -> dict:
    duration = receipt.get("duration_ms")
    return {"started_at": receipt.get("started_at"), "finished_at": receipt.get("finished_at"),
            "duration_ms": duration if type(duration) is int else None}


def _attempt(receipt: dict) -> dict:
    """One step attempt as a receipt recorded it. ``delta`` is whether the workspace
    version moved during the step; ``None`` when a version was not recorded."""
    before, after = receipt.get("workspace_version_before"), receipt.get("workspace_version_after")
    no_edits = receipt.get("no_edits") or {}
    return {
        "attempt": receipt.get("attempt"), "role": receipt.get("role"),
        **_ran(receipt.get("role_plan")),
        "session_id": receipt.get("session_id") or receipt.get("session"),
        "status": receipt.get("status") or receipt.get("state"),
        "success": receipt.get("success"),
        "recorded_at": receipt.get("recorded_at"),
        **_timings(receipt),
        "inputs": [_ref(r) for r in receipt.get("inputs") or []],
        "outputs": [_ref(r) for r in receipt.get("outputs") or []],
        "members": [{**{k: m.get(k) for k in ("role", "session_id", "success")},
                     **_ran(m.get("role_plan"))}
                    for m in receipt.get("members") or []],
        "delta": None if before is None or after is None else before != after,
        "no_edits": no_edits.get("result"),
        "stop": receipt.get("stop"),
        # The pause kind when this attempt stopped on one of the engine's resumable pauses
        # (deadline, silence, no progress, turn budget, refusal); ``None`` otherwise, so a
        # failed step is never drawn as paused.
        "pause": _attempt_pause(receipt),
        "turn_loop": _turn_loop(receipt),
        # What the attempt cost and how much context it used (#832); ``None`` for a
        # receipt written before it.
        "measure": receipt.get("measure") if isinstance(receipt.get("measure"), dict) else None,
    }


#: The stop code a usage-limit pause records (``flows.quota_recovery`` and the engine raise
#: it by this literal; neither exports a constant for it).
QUOTA_STOP = "flow.quota_wait"
QUOTA_WAIT = "waiting-for-quota"


def pause_kinds() -> dict[str, str]:
    """``{stop code: pause kind}`` for the stops a flow pauses on, from the engine's own
    constants: its liveness stops and a long step's loop stops, each kept only while the
    engine lists it in ``RESUMABLE_CHECKED_STOPS`` (so ``flow resume`` runs the step again).
    An ordinary failed step is resumable too, but it is a failure, not a pause."""
    from garuda.flows.engine import LIVENESS_STOPS, RESUMABLE_CHECKED_STOPS
    from garuda.flows.long_step import LOOP_STOPS

    kinds = {code: kind for kind, code in LIVENESS_STOPS.items()}
    for code in LOOP_STOPS:
        kinds.setdefault(code, code.removeprefix("flow.step_"))
    return {code: kind for code, kind in kinds.items() if code in RESUMABLE_CHECKED_STOPS}


def _attempt_pause(receipt: dict) -> str | None:
    stop = receipt.get("stop")
    code = stop.get("code") if isinstance(stop, dict) else None
    if receipt.get("status") != "stopped" or not isinstance(code, str):
        return None
    return pause_kinds().get(code)


def quota_limit(meta: dict) -> dict:
    """When a usage-limit pause resets, from the quota-recovery record (``reset_at``) or the
    stop's own cause (``cause.limit.resets_at``), with the limit window; ``None`` values
    when neither recorded it."""
    recovery = meta.get("quota_recovery") if isinstance(meta.get("quota_recovery"), dict) else {}
    stop = meta.get("flow_stop") if isinstance(meta.get("flow_stop"), dict) else {}
    cause = stop.get("cause") if isinstance(stop.get("cause"), dict) else {}
    limit = cause.get("limit") if isinstance(cause.get("limit"), dict) else {}

    def stamp(value):
        return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None

    reset = stamp(recovery.get("reset_at"))
    if reset is None:
        reset = stamp(limit.get("resets_at"))
    window = recovery.get("rate_limit_type") or limit.get("rate_limit_type")
    return {"reset_at": reset, "rate_limit_type": window if isinstance(window, str) else None,
            "source": recovery.get("quota_source") if isinstance(recovery.get("quota_source"), str)
            else None}


def flow_pause(meta: dict) -> dict | None:
    """Why a stopped flow is paused on a resumable stop, from its recorded fields, or ``None``:
    ``{"kind": "quota", ...}`` (with the reset time and window when recorded) for a
    usage-limit wait, else ``{"kind": <pause kind>}`` for a flow stopped on one of
    :func:`pause_kinds`. The recorded ``outcome`` is left as stored: the run ended without
    completing, and ``garuda flow resume`` continues it."""
    if meta.get("kind") != "flow":
        return None
    recovery = meta.get("quota_recovery")
    stop = meta.get("flow_stop")
    code = stop.get("code") if isinstance(stop, dict) else None
    if (isinstance(recovery, dict) and recovery.get("state") == QUOTA_WAIT) or code == QUOTA_STOP:
        # The reset time and window only when recorded (a Codex wait records neither).
        return {"kind": "quota", **{k: v for k, v in quota_limit(meta).items() if v is not None}}
    kinds = pause_kinds()
    if meta.get("flow_state") == "stopped" and code in kinds:
        return {"kind": kinds[code]}
    return None


def _turn_loop(receipt: dict) -> dict | None:
    """A long step's turn-loop facts as its receipt recorded them (#842), or ``None`` when
    it ran one plain turn. A count of the wrong type reads ``None`` (unknown), never 0."""
    if not any(key in receipt for key in ("turns", "rotations", "continuations", "repairs")):
        return None

    def count(value):
        return value if type(value) is int and value >= 0 else None

    repairs = receipt.get("repairs")
    natives = receipt.get("harness_sessions")
    return {"turns": count(receipt.get("turns")), "rotations": count(receipt.get("rotations")),
            "continuations": count(receipt.get("continuations")),
            "repairs": len(repairs) if isinstance(repairs, list) else None,
            "repair_outcomes": [r.get("outcome") for r in repairs if isinstance(r, dict)]
            if isinstance(repairs, list) else [],
            "harness_sessions": len(natives) if isinstance(natives, list) else None}


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


def flow(store, session_id: str, meta: dict | None = None, *, receipts: list[dict] | None = None) -> dict | None:
    """A flow session's steps, attempts, receipts and review outcome, or ``None``."""
    meta = meta if meta is not None else store.load_meta(session_id)
    if meta.get("kind") != "flow":
        return None
    from garuda.core.session_records import RecordError
    from garuda.core.starter_evidence import flow_receipt_shape, flow_steps, selected_flow_records

    declared = flow_steps(meta)
    declarations_known = declared is not None
    receipts_known = declarations_known
    journal_coverage = "unknown"
    if not declarations_known:
        declared, receipts = [], []
    if receipts is None:
        try:
            receipts, coverage = selected_flow_records(
                store, session_id, declared, expected_project_id=meta.get("project_id"),
            )
            receipts_known = (coverage["total_records"] is not None
                              and not coverage["unreadable_records"]
                              and not coverage["missing_records"])
            journal_coverage = coverage["journal"]
        except (RecordError, OSError, ValueError, TypeError):
            receipts = []
            receipts_known = False
    if not isinstance(receipts, list):
        receipts, receipts_known = [], False
    valid = [receipt for receipt in receipts if flow_receipt_shape(receipt, declared)]
    receipts_known = receipts_known and len(valid) == len(receipts)
    receipts = valid
    steps = []
    edges = []
    for step in declared:
        attempts = [_attempt(r) for r in receipts if r.get("step") == step]
        for attempt in attempts:
            for ref in attempt["inputs"]:
                # Which attempt consumed it, and of which producing attempt: two rounds pass
                # two different patches along the same pair of steps, and a list of edges
                # with nothing else to tell them apart read as a duplicate (#856 finding 2).
                edge = {"from_step": ref["producer_step"], "to_step": step, "type": ref["type"],
                        "digest": ref["digest"], "from_attempt": ref["attempt"],
                        "to_attempt": attempt["attempt"]}
                if edge not in edges:
                    edges.append(edge)
        steps.append({"id": step, "attempts": attempts})
    flow_meta = meta.get("flow")
    return {"name": flow_meta.get("name") if isinstance(flow_meta, dict) else None,
            "state": meta.get("flow_state"),
            "steps": steps, "edges": edges,
            "coverage": {"declarations": "complete" if declarations_known else "unknown",
                         "receipts": "complete" if receipts_known else "unknown",
                         "journal": journal_coverage},
            "stop": _flow_stop(meta.get("flow_stop")),
            # Own field: a review verdict is advice about the work, not verification of it.
            "review": meta.get("review"), "quota_recovery": meta.get("quota_recovery"),
            # How much of the change set each approving review saw (#851): partial, complete
            # or unknown, by reviewed step.
            "review_coverage": _review_coverage(meta.get("review_approvals")),
            # The receipts' per-attempt measures, totalled (#832): the same figure as
            # `flow show --json`'s summary.measure. Adapter-reported cost, never metered.
            "measure": _measure_total(steps)}


def _measure_total(steps: list[dict]) -> dict:
    from garuda.flows.measure import flow_total

    return flow_total([attempt for step in steps for attempt in step["attempts"]])


def _review_coverage(approvals) -> dict[str, str]:
    if not isinstance(approvals, dict):
        return {}
    return {str(step): (record.get("coverage") if record.get("coverage") in
                        ("partial", "complete", "unknown") else "unknown")
            for step, record in approvals.items() if isinstance(record, dict)}


def _bounded_fields(record: dict, limits) -> dict:
    return {key: (" ".join(str(record[key]).split())[:limit] if record.get(key) else None)
            for key, limit in limits}


def _flow_stop(stop) -> dict | None:
    """Why a stopped flow stopped: its recorded code, step, message and, for a failed
    step, the step session's own cause (#749), bounded."""
    if not isinstance(stop, dict) or not stop.get("code"):
        return None
    cause = stop.get("cause")
    limit = cause.get("limit") if isinstance(cause, dict) else None
    return {**_bounded_fields(stop, (("code", 120), ("step", 80), ("message", 500))),
            "cause": (_bounded_fields(cause, (("code", 120), ("detail", 500)))
                      if isinstance(cause, dict) and any(cause.get(k) for k in ("code", "detail"))
                      else None),
            # A usage-limit pause (flow.quota_wait): when the limit resets and its window.
            "limit": ({"resets_at": limit.get("resets_at")
                       if isinstance(limit.get("resets_at"), (int, float))
                       and not isinstance(limit.get("resets_at"), bool) else None,
                       "rate_limit_type": limit.get("rate_limit_type")
                       if isinstance(limit.get("rate_limit_type"), str) else None}
                      if isinstance(limit, dict) else None)}


def _spend(meta: dict) -> dict | None:
    """Project recorded native tree spend without reconstructing or repairing it.

    Missing evidence is distinct from a malformed snapshot. Known dollars remain
    visible as a lower bound when reservations or provenance leave cost unknown.
    """
    if "spend_gate" not in meta:
        return None
    snapshot = meta["spend_gate"]
    unreadable = {"state": "unreadable"}
    if not isinstance(snapshot, dict) or any(k not in snapshot for k in SPEND_REQUIRED_FIELDS):
        return unreadable
    # Refusals are projected but not inherited; their diagnostic count is optional too.
    optional = (*SPEND_OPTIONAL_COUNTERS, "refused")
    snapshot = {**snapshot, **{k: snapshot.get(k, 0) for k in optional}}
    counters = ("settled_tokens", "cost_unknown_calls", *optional)
    if any(type(snapshot.get(k)) is not int or snapshot[k] < 0 for k in counters):
        return unreadable
    if type(snapshot.get("prior_spend_unknown")) is not bool:
        return unreadable

    def dollars(value):
        return (type(value) is int and value >= 0) or (
            type(value) is float and value >= 0 and math.isfinite(value))

    if not dollars(snapshot.get("settled_cost_usd")):
        return unreadable
    cap = snapshot.get("max_tokens")
    if "max_tokens" not in snapshot or (cap is not None and (type(cap) is not int or cap <= 0)):
        return unreadable
    cap = snapshot.get("max_cost_usd")
    if "max_cost_usd" not in snapshot or (cap is not None and (not dollars(cap) or cap <= 0)):
        return unreadable
    inherited = None
    if "spend_inherited" in meta:
        raw = meta["spend_inherited"]
        if not isinstance(raw, dict) or raw.get("source") not in (
                "unavailable", "spend_gate", "spend_checkpoint"):
            return unreadable
        keys = ("source", "inherited_tokens", "inherited_cost_usd", "inherited_cost_unknown_calls",
                "interrupted_calls", "interrupted_tokens_charged")
        if raw["source"] == "unavailable":
            if raw.get("inherited_tokens") is not None or raw.get("inherited_cost_usd") is not None:
                return unreadable
        elif (type(raw.get("inherited_tokens")) is not int or raw["inherited_tokens"] < 0
              or not dollars(raw.get("inherited_cost_usd"))
              or type(raw.get("inherited_cost_unknown_calls")) is not int
              or raw["inherited_cost_unknown_calls"] < 0):
            return unreadable
        for key in ("inherited_cost_unknown_calls", "interrupted_calls", "interrupted_tokens_charged"):
            if key in raw and (type(raw[key]) is not int or raw[key] < 0):
                return unreadable
        inherited = {k: raw[k] for k in keys if k in raw}
    unknown = (snapshot["prior_spend_unknown"] or any(snapshot[k] > 0 for k in (
        "reserved_calls", "cost_unknown_calls", "interrupted_calls", "unknown_outcome_calls",
        "unreported_failed_attempts")) or (inherited is not None and (
            inherited["source"] == "unavailable" or inherited.get("inherited_cost_unknown_calls", 0) > 0)))
    return {"state": "recorded", "scope": "native_run_tree",
            **{k: snapshot[k] for k in (*counters, "max_tokens", "max_cost_usd",
                                      "settled_cost_usd", "prior_spend_unknown")},
            "cost_usd": UNKNOWN if unknown else snapshot["settled_cost_usd"],
            "cost_status": UNKNOWN if unknown else "reported",
            "inherited": inherited}


def session_row(store, meta: dict, *, queue_index: dict | None = None, liveness=None) -> dict[str, Any]:
    """The shared row for one session."""
    session_id = meta.get("session_id") or ""
    state = effective_state(meta, liveness=liveness)
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
        "origin": meta.get("origin"),
        "background": bool(meta.get("background")),
        "task": meta.get("task"),
        "agent": meta.get("agent"),
        "agent_digest": meta.get("agent_digest"),
        "agent_segment": meta.get("agent_segment"),
        "model": meta.get("model"),
        # What the agent reported running, beside ``model`` (``acp:<harness>`` for an
        # ACP session); ``None`` for a native session or an agent that did not say (#752).
        "reported_model": reported_model(meta),
        "runtime": runtime or ("flow" if meta.get("kind") == "flow" else "native"),
        "role": meta.get("role"),
        "status": meta.get("status"),
        "state": {k: state.get(k) for k in ("process", "work", "outcome")},
        "label": summary_label(state),
        "crashed": is_crashed(state),
        # A stopped flow on a resumable pause (``flow_pause``); ``None`` while work is
        # active and for every other session. ``label`` and ``outcome`` stay as recorded.
        "pause": None if state.get("work") in ACTIVE_WORK else flow_pause(meta),
        "verification": verification,
        "self_check": state.get("self_check"),
        "workspace": meta.get("workspace"),
        "isolation": meta.get("isolation") or "shared",
        "workspace_kind": meta.get("workspace_kind"),
        "branch": meta.get("branch"),
        "queue": queue,
        "worker": {"pid": worker.get("pid")} if worker else None,
        "resumed_from": meta.get("resumed_from"),
        "quota_recovery": meta.get("quota_recovery"),
        "usage": UNKNOWN,
        "cost": UNKNOWN,
        "spend": _spend(meta),
        "created_at": meta.get("created_at"),
        "updated_at": meta.get("updated_at"),
    }


def listed_row(store, meta: dict, *, queue_index: dict | None = None) -> dict[str, Any]:
    """One list row: the shared row plus its pending approvals, live ones counted apart
    (an expired request is denied by the broker, so it is not waiting on anyone)."""
    row = session_row(store, meta, queue_index=queue_index)
    pending = approvals(store, row["session_id"]) \
        if row["state"]["work"] in ("queued", "working", "waiting") else []
    row["approvals_pending"] = len(pending)
    row["approvals_live"] = sum(1 for a in pending if not a.get("expired"))
    return row


def sessions(store, *, limit: int | None = 20, queue=None) -> list[dict]:
    index = _queue_index(queue)
    return [listed_row(store, meta, queue_index=index) for meta in store.list_sessions(limit=limit)]


def trace_detail(meta: dict) -> str | None:
    """The recorded ``observability.trace_detail`` level of a session's ACP events
    (#691): ``metadata``, ``commands``, or ``None`` when it was not recorded."""
    from garuda.acp.normalize import TRACE_LEVELS

    value = meta.get("trace_detail")
    return value if isinstance(value, str) and value in TRACE_LEVELS else None


def session(store, session_id: str, *, queue=None) -> dict:
    meta = store.load_meta(session_id)
    # One queue read feeds both the row and the ownership projection.
    entries = _queue_entries(queue) if queue is not None else None
    row = session_row(store, meta, queue_index={} if queue is None else _index_entries(entries))
    if meta.get("forked_from") is not None:
        row["context_fork"] = {key: meta.get(key) for key in (
            "forked_from", "fork_at", "fork_requested_at", "fork_adjustment", "source_digest",
            "source_files", "rewind", "filesystem_restored", "processes_restored", "inherited_evidence", "references",
            "receipt_references", "receipt_reference_coverage",
        )}
    from garuda.observability.usage import reported_usage

    # An external harness's own usage/cost report, labelled as such; ``usage``/``cost``
    # stay Garuda's accounting (#691).
    row["reported_usage"] = reported_usage(session_id)
    row["approvals"] = approvals(store, session_id)
    row["approvals_pending"] = len(row["approvals"])
    row["approvals_live"] = sum(1 for a in row["approvals"] if not a.get("expired"))
    from garuda.core.approval_outcome import approval_summary

    # Denied tool calls apart from the outcome: a blocked run can end `completed` (#691).
    row["approval_summary"] = approval_summary(meta)
    acp_session = meta.get("acp_session")
    row["acp_session"] = acp_session if isinstance(acp_session, dict) else None
    # The study-trace level its ACP events were persisted at; None before #691 recorded it.
    row["trace_detail"] = trace_detail(meta)
    role = meta.get("role") if isinstance(meta.get("role"), dict) else {}
    # What the run ran with, beside what was offered (#726).
    row["acp_mode"] = meta.get("acp_mode") or role.get("effective_mode")
    adapter = role.get("adapter") if isinstance(role.get("adapter"), dict) else {}
    segment = meta.get("agent_segment") if isinstance(meta.get("agent_segment"), dict) else {}
    reported = (acp_session or {}).get("agent_info") if isinstance(acp_session, dict) else None
    row["adapter"] = ({"runtime_id": adapter.get("runtime_id") or segment.get("runtime"),
                       "version": adapter.get("version"),
                       "reported_agent": segment.get("reported_agent"),
                       "reported_version": segment.get("reported_version"),
                       "agent_info": reported}
                      if adapter or segment.get("kind") == "acp_execution" else None)
    selection = meta.get("initial_selection")
    row["initial_selection"] = selection if isinstance(selection, dict) else None
    row["error"] = ({"code": meta.get("error_code"), "detail": meta.get("error_detail")}
                    if meta.get("error_code") else None)
    row["approval_decisions"] = [
        {field: value.get(field) for field in ("approval_id", "tool_kind", "outcome", "reason")}
        for key, value in meta.items()
        if key.startswith("approval:") and isinstance(value, dict)
        and value.get("via") == "confined-readonly"
    ]
    from garuda.core.ownership_read_model import project as project_ownership

    # Joins leases, queue, capacity, children and quota without probing beyond the row's
    # own owner liveness; reconciliation is reported, never performed (#376).
    row["ownership"] = project_ownership(
        row, meta, approvals_pending=row["approvals_pending"], queue=queue,
        queue_entries=entries,
    )
    row["flow"] = flow(store, session_id, meta)
    # A flow's own session totals its step sessions' adapter-reported usage (#755).
    row["step_reported_usage"] = None
    if row["flow"] is not None:
        from garuda.flows.trajectory import steps_reported_usage

        row["step_reported_usage"] = steps_reported_usage(row["flow"])
    from garuda.consult import view
    from garuda.core import conversation

    consults = conversation.consult_rows(store, session_id, meta)
    row["consults"] = {"summary": view.summary(consults), "line": view.line(consults),
                       "entries": consults}
    try:
        from garuda.core.artifact_read_model import project_session_artifacts

        row["artifacts"] = project_session_artifacts(
            store, session_id, expected_project_id=meta.get("project_id")
        )
    except (ValueError, OSError) as exc:
        row["artifacts"] = {
            "schema": "garuda.artifact-projection",
            "version": 1,
            "references": [],
            "omitted_references": 0,
            "coverage": "unavailable",
            "reason": str(exc),
        }
    try:
        row["failure_evidence"] = failure_evidence(store, session_id)
    except ValueError as exc:
        from garuda.core.failure_evidence import unavailable_projection

        row["failure_evidence"] = unavailable_projection(str(exc))
    from garuda.observability.lanes import ACP_EVENTS_FILE

    # Which logs the session has: a flow has no native event log of its own and an
    # external harness writes only its own trail, so "no events" is not "did nothing".
    row["event_log"] = {
        "native": Path(store.events_path(session_id)).is_file(),
        "external": (Path(store.session_dir(session_id)) / ACP_EVENTS_FILE).is_file(),
    }
    row.update(session_result(store, session_id, meta, row))
    return row


#: How many files a session's detail lists from the agent's recorded working state.
MAX_CHANGED_FILES = 50


def _stamp(value):
    from datetime import datetime, timezone

    # Parsed here, not with the benchmark package's helper (agent code never imports it):
    # ISO 8601, with a trailing Z, is what every session writer stamps.
    try:
        parsed = (datetime.fromisoformat(value.replace("Z", "+00:00"))
                  if isinstance(value, str) else None)
    except ValueError:
        parsed = None
    if parsed is not None and parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def flow_progress(store, meta: dict) -> dict | None:
    """The step a running flow has launched and not yet settled, from its journal.

    ``None`` when the flow declares no readable steps or no step is in flight (between
    steps, or in its acceptance phase); a reader then shows no step rather than a guess.
    """
    from garuda.core.starter_evidence import flow_steps
    from garuda.flows.engine import unsettled_attempts

    declared = flow_steps(meta)
    if not declared:
        return None
    try:
        pending = unsettled_attempts(store, meta.get("session_id") or "")
    except (OSError, ValueError, TypeError):
        return None
    if not pending:
        return None
    step, attempt = pending[-1]
    if step not in declared:
        return None
    return {"step": step, "attempt": attempt, "index": declared.index(step) + 1,
            "total": len(declared)}


def session_result(store, session_id: str, meta: dict, row: dict) -> dict[str, Any]:
    """What a session's detail leads with beside its state: the task's result and timing.

    * ``starter_origin``: admitted from a Starter (its meta carries the ``starter`` record).
    * ``final_message``: the stored summary (clipped to 2,000 characters when written),
      redacted; ``None`` when none was stored or the output was withheld.
    * ``files_changed``: the files the agent's working state recorded it wrote; ``None``
      when it recorded none (an external harness, a flow, an older run).
    * ``ended_at`` / ``duration_ms``: a finished session's recorded end (``updated_at``, or
      for a flow the later of that and its step receipts) and the time from ``created_at``;
      ``None`` while it runs or when no later end was recorded.
    * ``flow_progress``: a running flow's in-flight step (see :func:`flow_progress`).
    """
    from garuda.context.redact import redact_text
    from garuda.runtime.session_state import ACTIVE_WORK

    withheld = meta.get("outputs_withheld") is True
    message = meta.get("final_message")
    final = redact_text(message)[0] \
        if isinstance(message, str) and message.strip() and not withheld else None
    try:
        files = store.load_state(session_id).get("files_modified")
    except (OSError, ValueError, AttributeError):
        files = None
    files_changed = None
    if isinstance(files, list) and files and all(isinstance(f, str) for f in files):
        files_changed = {"files": files[:MAX_CHANGED_FILES], "total": len(files),
                         "source": "working state"}
    active = row["state"].get("work") in ACTIVE_WORK
    ended_at = duration = None
    if not active:
        end_text = meta.get("updated_at")
        for step in (row.get("flow") or {}).get("steps") or []:
            for attempt in step.get("attempts") or []:
                for key in ("finished_at", "recorded_at"):
                    stamp = _stamp(attempt.get(key))
                    if stamp is not None and (_stamp(end_text) is None or stamp > _stamp(end_text)):
                        end_text = attempt[key]
        start, end = _stamp(meta.get("created_at")), _stamp(end_text)
        if start is not None and end is not None and end > start:
            ended_at, duration = end_text, int((end - start).total_seconds() * 1000)
    return {
        "starter_origin": meta.get("starter") is not None,
        "final_message": final,
        "output_withheld": withheld,
        "files_changed": files_changed,
        "ended_at": ended_at,
        "duration_ms": duration,
        "flow_progress": flow_progress(store, meta) if active and meta.get("kind") == "flow"
        else None,
    }


def failure_evidence(store, session_id: str, *, expected_project_id: str | None = None) -> dict:
    """The one bounded causal projection used by every public interface."""
    from garuda.core.failure_evidence import SessionFailureEvidence

    return SessionFailureEvidence(store).project(
        session_id, expected_project_id=expected_project_id
    )


def starter_result(store, session_id: str, *, limit: int = 50, offset: int = 0) -> dict:
    """Selected starter evidence with bounded coverage and no process probes."""
    from garuda.core.starter_read_model import project

    return project(store, session_id, limit=limit, offset=offset)
