"""Feeding the usage ledger from native runs (plan task E.1, #168).

:class:`NativeUsageObserver` watches an :class:`~garuda.core.events.EventStore` and writes
one ``native_model_call`` per model call recorded in it, exactly the events
``aggregate_model_metrics`` counts, so a session's ledger totals equal its metrics.
Because a record's key is ``native:<session>:<event index>``, the same call can be
presented again — by a retry of the append, by :func:`reconcile` reading the persisted
log after a crash — and is written once.

Role, origin (``run``, ``subagent``, ``flow``, ``consult``) and the original
``call_purpose`` are stored as separate dimensions; nothing but identities and counts
reaches the ledger.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from garuda.observability import ledger as ledger_module
from garuda.observability.ledger import NATIVE, Ledger

logger = logging.getLogger(__name__)


def _parse_time(stamp: Any) -> float:
    try:
        return datetime.fromisoformat(str(stamp)).timestamp()
    except ValueError:
        return time.time()


def _usage_fields(usage: dict) -> dict:
    def count(*names):
        for name in names:
            value = usage.get(name)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                return value
        return None

    cost = usage.get("cost_usd")
    return {
        "input_tokens": count("prompt_tokens", "input_tokens"),
        "output_tokens": count("completion_tokens", "output_tokens"),
        "total_tokens": count("total_tokens"),
        "cache_tokens": count("cache_read_input_tokens", "cached_tokens"),
        "cost_usd": float(cost) if isinstance(cost, (int, float)) and not isinstance(cost, bool)
        and cost >= 0 else None,
    }


def native_records(event: dict, index: int, context: dict) -> list[dict]:
    """The ledger records one event stands for (none for events that are not model calls)."""
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    session = event.get("session_id")
    when = _parse_time(event.get("timestamp"))
    base = {"kind": NATIVE, "time": when, "session_id": session, **context}
    if event.get("type") == "model_response":
        if payload.get("truncated") and not payload.get("usage"):
            return []
        usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
        return [{**base, "key": f"native:{session}:{index}", **_usage_fields(usage),
                 "model": payload.get("model"), "binding_role": payload.get("model_binding_role"),
                 "call_purpose": payload.get("call_purpose"),
                 "calls": max(1, int(payload.get("model_calls", 1) or 1)),
                 "duration_ms": payload.get("duration_ms"), "accounting": payload.get("accounting")}]
    if event.get("type") == "collection" and isinstance(payload.get("attempt_metrics"), list):
        out = []
        for position, attempt in enumerate(payload["attempt_metrics"]):
            if not isinstance(attempt, dict):
                continue
            out.append({**base, "key": f"native:{session}:{index}:{position}",
                        **_usage_fields(attempt.get("usage") or {}), "model": attempt.get("model"),
                        "binding_role": attempt.get("model_binding_role"),
                        "call_purpose": attempt.get("call_purpose"),
                        "calls": max(1, int(attempt.get("model_calls", 1) or 1)),
                        "duration_ms": attempt.get("elapsed_ms")})
        return out
    return []


def _clean(records: list[dict]) -> list[dict]:
    """Drop fields that do not fit the schema's value rules rather than lose the record
    (a model id with a space, say): unknown stays ``null``."""
    out = []
    for record in records:
        fixed = {}
        for name, value in record.items():
            check = ledger_module.SCHEMA[NATIVE].get(name)
            fixed[name] = value if check is None or check(value) else None
        out.append(fixed)
    return out


class NativeUsageObserver:
    """Writes the ledger records for an event store's model calls as they happen."""

    def __init__(self, ledger: Ledger, store, *, root_id: str, origin: str = "run",
                 role_lookup=None):
        self._ledger = ledger
        self._store = store
        self._root_id = root_id
        self._origin = origin
        self._role_lookup = role_lookup
        self._context: dict[str, dict] = {}

    def _context_for(self, session_id: str) -> dict:
        cached = self._context.get(session_id)
        if cached is not None and cached.get("project_id"):
            return cached
        context = {"root_id": self._root_id,
                   "origin": "run" if session_id == self._root_id else "subagent"}
        if self._origin != "run" and session_id == self._root_id:
            context["origin"] = self._origin
        try:
            meta = self._store.load_meta(session_id)
        except Exception:
            meta = {}
        context["project_id"] = meta.get("project_id")
        role = meta.get("role")
        # RolePlan.record() stores the role's name under "name".
        context["role"] = role.get("name") if isinstance(role, dict) else None
        step = meta.get("flow_step")
        if isinstance(step, dict):
            context["flow_step"] = step.get("step")
            context["attempt"] = step.get("attempt")
            if session_id == self._root_id:
                context["origin"] = "flow"
        if meta.get("origin") == "consult":
            context["origin"] = "consult"
        context["harness"] = "native"
        self._context[session_id] = context
        return context

    def __call__(self, event: dict, events) -> None:
        if event.get("type") not in ("model_response", "collection"):
            return
        records = native_records(event, events.count() - 1,
                                 {k: v for k, v in self._context_for(event["session_id"]).items()
                                  if v is not None})
        if records:
            self._ledger.append_many(_clean(records))


def attach(events, store, *, ledger: Ledger | None = None, origin: str = "run") -> None:
    """Record ``events``' model calls to the usage ledger. Never raises: accounting must not
    stop a run (a ledger that cannot be written leaves the session marked incomplete)."""
    try:
        events.add_observer(NativeUsageObserver(ledger or Ledger(), store,
                                                root_id=events.session_id, origin=origin))
    except Exception:
        logger.warning("usage ledger unavailable", exc_info=True)


def reconcile(store, session_id: str, *, ledger: Ledger | None = None) -> int:
    """Write any records a session's persisted log implies that the ledger lacks (after a
    crash, or an observer that could not write). Idempotent; returns how many were added."""
    path = Path(store.events_path(session_id))
    if not path.is_file():
        return 0
    observer = NativeUsageObserver(ledger or Ledger(), store, root_id=session_id)
    context = {k: v for k, v in observer._context_for(session_id).items() if v is not None}
    added = 0
    for index, line in enumerate(path.read_text(encoding="utf-8").splitlines()):
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and event.get("type") in ("model_response", "collection"):
            added += observer._ledger.append_many(_clean(native_records(event, index, context)))
    return added


def record_fallback_start(store, session_id: str, *, ledger: Ledger | None = None) -> bool:
    """Record, once, that ``session_id`` started on a fallback candidate (C.9's decision,
    recorded on the session's role before it started). Not an inference attempt."""
    try:
        meta = store.load_meta(session_id)
        decision = (meta.get("role") or {}).get("fallback")
        taken = (decision or {}).get("taken") or {}
        if not decision or not taken.get("index"):
            return False
        reasons = [s.get("reason") for s in decision.get("skipped", []) if s.get("reason")]
        return (ledger or Ledger()).append({
            "kind": ledger_module.FALLBACK, "key": f"fallback:{session_id}",
            "time": time.time(), "session_id": session_id, "project_id": meta.get("project_id"),
            "role": (meta.get("role") or {}).get("name"),
            "from_harness": (decision.get("primary") or {}).get("harness"),
            "to_harness": taken.get("harness"), "reason": reasons[0] if reasons else None})
    except Exception:
        logger.warning("fallback_start not recorded", exc_info=True)
        return False
