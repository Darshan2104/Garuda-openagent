"""Usage statistics over the ledger (plan task F.3, #169).

Pure arithmetic over :class:`~garuda.observability.ledger.Ledger` records. Statistics are a
**report**: they are never an input to routing or fallback, and the view says so.

Ranges are rolling UTC windows (24h, 7d, 30d) ending at ``now``; the time zone only affects
how a client displays them. A record is in a range when ``start <= time < end``.

Units are kept apart, and a percentage only ever compares like with like:

* ``native_calls`` — inference attempts Garuda made (``native_model_call``);
* ``acp_turns`` — proved per-turn or cumulative-derived deltas an external harness reported
  (``acp_usage_delta``);
* ``snapshots`` — occupancy / running-total reports (``acp_usage_snapshot``), counted only to
  say they were left out.

The work-type share is *native calls ÷ native calls*; ACP turns and snapshots are not in its
denominator, and the report lists what was excluded. Cost is never zero-filled: a range whose
records are all unpriced reads ``unknown``, a mixed range reports the known sum and the number
of unpriced records separately.

Exports hold the fixed schema fields only (identities and counts), in a fixed order.
"""

from __future__ import annotations

import csv
import io
import json
import statistics
from datetime import datetime, timezone
from typing import Any

RANGES = {"24h": 86400.0, "7d": 7 * 86400.0, "30d": 30 * 86400.0}
EXPORT_FIELDS = ("time", "kind", "session_id", "project_id", "role", "origin", "call_purpose",
                 "harness", "model", "calls", "input_tokens", "output_tokens", "total_tokens",
                 "cache_tokens", "cost_usd", "duration_ms")
NOT_A_ROUTING_INPUT = "Statistics are a report. They are never used to choose a harness or model."


def window(range_name: str, now: float) -> tuple[float, float]:
    if range_name not in RANGES:
        raise ValueError(f"range must be one of {', '.join(RANGES)}")
    return now - RANGES[range_name], now


def in_range(records, range_name: str, now: float) -> list[dict]:
    start, end = window(range_name, now)
    return [r for r in records if r.get("kind") in
            ("native_model_call", "acp_usage_delta", "acp_usage_snapshot")
            and isinstance(r.get("time"), (int, float)) and start <= r["time"] < end]


def _cost(records: list[dict]) -> dict:
    counted = [r for r in records if r["kind"] != "acp_usage_snapshot"]
    known = [r["cost_usd"] for r in counted if r.get("cost_usd") is not None]
    if not counted:
        state = "none"
    elif not known:
        state = "unknown"
    elif len(known) < len(counted):
        state = "partial"
    else:
        state = "known"
    return {"state": state, "known_usd": round(sum(known), 8) if known else None,
            "unpriced_records": len(counted) - len(known)}


def _measure(records: list[dict]) -> dict:
    native = [r for r in records if r["kind"] == "native_model_call"]
    deltas = [r for r in records if r["kind"] == "acp_usage_delta"]
    counted = native + deltas
    return {
        "native_calls": sum(r.get("calls", 1) for r in native),
        "acp_turns": len(deltas),
        "input_tokens": sum(r.get("input_tokens") or 0 for r in counted),
        "output_tokens": sum(r.get("output_tokens") or 0 for r in counted),
        "total_tokens": sum((r.get("total_tokens") if r.get("total_tokens") is not None
                             else (r.get("input_tokens") or 0) + (r.get("output_tokens") or 0))
                            for r in counted),
        "cost": _cost(records),
    }


def _group(records: list[dict], key) -> dict:
    out: dict = {}
    for record in records:
        if record["kind"] == "acp_usage_snapshot":
            continue
        out.setdefault(key(record), []).append(record)
    return out


def stats(records, range_name: str, now: float) -> dict[str, Any]:
    start, end = window(range_name, now)
    rows = in_range(records, range_name, now)
    snapshots = [r for r in rows if r["kind"] == "acp_usage_snapshot"]
    counted = [r for r in rows if r["kind"] != "acp_usage_snapshot"]
    native = [r for r in rows if r["kind"] == "native_model_call"]
    total_calls = sum(r.get("calls", 1) for r in native)

    work = []
    for work_type, rs in sorted(_group(native, lambda r: r.get("call_purpose") or "unknown").items()):
        calls = sum(r.get("calls", 1) for r in rs)
        work.append({"work_type": work_type, "native_calls": calls,
                     "share": round(calls / total_calls, 6) if total_calls else None})

    def table(key_name, key):
        out = []
        for value, rs in sorted(_group(rows, key).items(), key=lambda kv: str(kv[0])):
            out.append({key_name: value, **_measure(rs)})
        return out

    harness_model = []
    for (harness, model), rs in sorted(_group(rows, lambda r: (
            r.get("harness") or ("native" if r["kind"] == "native_model_call" else "unknown"),
            r.get("model") or "not reported")).items(), key=lambda kv: str(kv[0])):
        harness_model.append({"harness": harness, "model": model, **_measure(rs)})

    days = {}
    for record in counted:
        day = datetime.fromtimestamp(record["time"], timezone.utc).strftime("%Y-%m-%d")
        days.setdefault(day, []).append(record)
    daily = [{"date": day, **_measure(rs)} for day, rs in sorted(days.items())]

    tokens_per_call = [r.get("total_tokens") if r.get("total_tokens") is not None
                       else (r.get("input_tokens") or 0) + (r.get("output_tokens") or 0)
                       for r in native]
    durations = [r["duration_ms"] for r in native if r.get("duration_ms") is not None]
    return {
        "range": range_name, "start": start, "end": end,
        "note": NOT_A_ROUTING_INPUT,
        "measures": _measure(rows),
        "excluded": {"snapshots": len(snapshots),
                     "reason": "snapshots are occupancy or running totals, never summed"},
        "coverage": {"native_calls_with_unknown_cost": sum(
            r.get("calls", 1) for r in native if r.get("cost_usd") is None),
            "native_calls": total_calls},
        "work_type_share": {"unit": "native calls", "denominator": total_calls,
                            "excluded_acp_turns": sum(1 for r in rows if r["kind"] == "acp_usage_delta"),
                            "rows": work},
        "harness_model": harness_model,
        "daily": daily,
        "by_role": table("role", lambda r: r.get("role") or "unknown"),
        "by_project": table("project_id", lambda r: r.get("project_id") or "unknown"),
        "median": {
            "tokens_per_native_call": statistics.median(tokens_per_call) if tokens_per_call else None,
            "native_call_duration_ms": statistics.median(durations) if durations else None,
        },
    }


def export_rows(records, range_name: str, now: float) -> list[dict]:
    """The schema fields of each counted record in the range, in a fixed order."""
    return [{name: record.get(name) for name in EXPORT_FIELDS}
            for record in sorted(in_range(records, range_name, now),
                                 key=lambda r: (r["time"], r.get("key", "")))]


def export_csv(rows: list[dict]) -> str:
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=EXPORT_FIELDS, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({k: ("" if v is None else v) for k, v in row.items()})
    return out.getvalue()


def export_json(rows: list[dict]) -> str:
    return json.dumps({"fields": list(EXPORT_FIELDS), "rows": rows}, sort_keys=False)
