"""What one flow step attempt cost and how much context it used (#832).

A step attempt's receipt carries a ``measure`` record built by :func:`attempt`:
the assembled prompt's size split by part (``chars``, and ``est_tokens`` =
chars / 4, an estimate, not a tokenizer count), the step sessions it used,
their turns and tool calls, its wall time and the harness's own usage
reports summarized (:func:`garuda.observability.usage.usage_summary`: how many
reports, first and peak ``context_used``, the last ``context_size``, the
reported cumulative ``cost_usd`` and model). Everything here is read from what
Garuda already persisted; nothing here raises: a session that cannot be read
measures as unknown (``None``), never as zero.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

VERSION = 1
ESTIMATE = "est_tokens is chars / 4, an estimate; not a tokenizer count"
#: Bytes of one session's event trail read at most to count its turns and tool calls.
MAX_TRAIL_BYTES = 16 * 1024 * 1024
#: Lifecycle states that close an ACP turn.
_TURN_ENDS = ("completed", "failed", "cancelled")


def est_tokens(chars: int) -> int:
    return (chars + 3) // 4


def prompt_record(prompt: str, parts: list[dict] | None) -> dict:
    """The prompt's size and its parts' sizes (which add up to the prompt's)."""
    out: dict[str, Any] = {"chars": len(prompt), "est_tokens": est_tokens(len(prompt))}
    if parts is not None:
        out["parts"] = [{**part, "est_tokens": est_tokens(part["chars"])} for part in parts]
    return out


def _trail_lines(path: Path):
    with path.open("rb") as handle:
        data = handle.read(MAX_TRAIL_BYTES + 1)
    lines = data[:MAX_TRAIL_BYTES].splitlines()
    if len(data) > MAX_TRAIL_BYTES and lines:
        lines.pop()  # a line cut at the byte bound is not a record
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            yield row


def activity(store, session_id: str) -> dict:
    """A step session's turns and tool calls, from its persisted event trail.

    An ACP session counts its ``outbound_prompt`` events (one per turn) and its
    distinct tool call ids; a native session counts its model responses
    (``turns`` is then model calls) and tool calls. ``None`` when unknown.
    """
    out: dict[str, Any] = {"trail": None, "turns": None, "tool_calls": None,
                           "stop_reason": None, "approval_denied": None}
    try:
        acp = Path(store.session_dir(session_id)) / "acp-events.jsonl"
        if acp.is_file():
            turns, calls, stop, denied = 0, set(), None, None
            for row in _trail_lines(acp):
                payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
                if row.get("kind") == "outbound_prompt":
                    turns += 1
                elif row.get("kind") == "tool_call":
                    calls.add(str(payload.get("tool_call_id") or f"seq:{row.get('seq')}"))
                elif row.get("kind") == "lifecycle" and payload.get("state") in _TURN_ENDS:
                    # The turn's closing event: the harness's raw stop reason and
                    # whether an approval was denied in it (#824).
                    raw = payload.get("raw_stop_reason") or payload.get("stop_reason")
                    stop = raw[:64] if isinstance(raw, str) else stop
                    if isinstance(payload.get("approval_denied"), bool):
                        denied = payload["approval_denied"]
            return {"trail": "acp", "turns": turns, "tool_calls": len(calls),
                    "stop_reason": stop, "approval_denied": denied}
        native = Path(store.events_path(session_id))
        if native.is_file():
            turns = calls = 0
            for row in _trail_lines(native):
                if row.get("type") == "model_response":
                    turns += 1
                elif row.get("type") == "tool_call":
                    calls += 1
            return {**out, "trail": "native", "turns": turns, "tool_calls": calls}
    except Exception:
        return out
    return out


def session_record(store, session_id: str) -> dict:
    from garuda.observability.usage import usage_summary

    record: dict[str, Any] = {"session_id": session_id, **activity(store, session_id)}
    try:
        meta = store.load_meta(session_id)
    except Exception:
        meta = None
    record["usage"] = usage_summary(meta) if isinstance(meta, dict) else None
    return record


def _sum(values: list) -> Any:
    known = [v for v in values if isinstance(v, int)]
    return sum(known) if known and len(known) == len(values) else None


def combine(usages: list[dict | None]) -> dict | None:
    """Several sessions' usage summaries as one (peak is the largest, cost the sum)."""
    known = [u for u in usages if isinstance(u, dict)]
    if not known:
        return None
    peaks = [u["peak_context_used"] for u in known if isinstance(u.get("peak_context_used"), int)]
    costs = [u["cost_usd"] for u in known if isinstance(u.get("cost_usd"), (int, float))]
    sizes = [u["context_size"] for u in known if isinstance(u.get("context_size"), int)]
    firsts = [u["first_context_used"] for u in known
              if isinstance(u.get("first_context_used"), int)]
    return {
        "source": "adapter_reported",
        "reports": sum(u.get("reports") or 0 for u in known),
        "first_context_used": firsts[0] if firsts else None,
        "peak_context_used": max(peaks) if peaks else None,
        "context_size": max(sizes) if sizes else None,
        "cost_usd": round(sum(costs), 8) if costs else None,
        "cost_complete": bool(costs) and len(known) == len(usages)
        and all(u.get("cost_complete") for u in known),
        "models": sorted({m for u in known for m in u.get("models") or []}),
    }


def _bounded(value: Any, limit: int = 64) -> str | None:
    return value[:limit] if isinstance(value, str) and value else None


def attempt(store, *, prompt: str, parts: list[dict] | None, session_ids: list,
            duration_ms: int | None, outcomes: list | None = None) -> dict:
    """The ``measure`` record of one step attempt. Never raises.

    ``outcomes`` are the launcher's results (``StepResult``) for ``session_ids``:
    their ``stop_reason`` (the harness's raw ACP stop reason) and ``liveness``
    cause (``deadline`` / ``silence``, #828) are kept with each session."""
    try:
        sessions = [session_record(store, sid) for sid in session_ids if sid]
    except Exception:
        sessions = []
    by_session = {getattr(o, "session_id", None): o for o in outcomes or []}
    for session in sessions:
        outcome = by_session.get(session["session_id"])
        session["stop_reason"] = (_bounded(getattr(outcome, "stop_reason", None))
                                  or session.get("stop_reason"))
        liveness = getattr(outcome, "liveness", None)
        session["liveness"] = liveness if liveness in ("deadline", "silence") else None
    stops = [s["stop_reason"] for s in sessions if s.get("stop_reason")]
    denied = [s["approval_denied"] for s in sessions if isinstance(s.get("approval_denied"), bool)]
    causes = [s["liveness"] for s in sessions if s.get("liveness")]
    return {
        "version": VERSION, "estimate": ESTIMATE,
        "prompt": prompt_record(prompt, parts),
        "sessions": len(sessions),
        "turns": _sum([s["turns"] for s in sessions]) if sessions else 0,
        "tool_calls": _sum([s["tool_calls"] for s in sessions]) if sessions else 0,
        "duration_ms": duration_ms if type(duration_ms) is int else None,
        "usage": combine([s["usage"] for s in sessions]) if sessions else None,
        # The last session's raw harness stop reason; whether any denied an approval.
        "stop_reason": stops[-1] if stops else None,
        "approval_denied": any(denied) if denied else None,
        "liveness": causes[-1] if causes else None,
        "per_session": sessions,
    }


def flow_total(attempts: list[dict]) -> dict:
    """The measured attempts of a flow, totalled: sessions, turns, tool calls, the
    estimated prompt tokens, the largest peak context and the reported cost."""
    measured = [a["measure"] for a in attempts if isinstance(a.get("measure"), dict)]
    usages = [m.get("usage") for m in measured]
    peaks = [u["peak_context_used"] for u in usages
             if isinstance(u, dict) and isinstance(u.get("peak_context_used"), int)]
    costs = [u["cost_usd"] for u in usages
             if isinstance(u, dict) and isinstance(u.get("cost_usd"), (int, float))]
    return {
        "source": "receipts",
        "attempts": len(attempts), "measured_attempts": len(measured),
        "sessions": sum(m.get("sessions") or 0 for m in measured),
        "turns": _sum([m.get("turns") for m in measured]) if measured else None,
        "tool_calls": _sum([m.get("tool_calls") for m in measured]) if measured else None,
        "prompt_chars": sum((m.get("prompt") or {}).get("chars") or 0 for m in measured),
        "prompt_est_tokens": sum((m.get("prompt") or {}).get("est_tokens") or 0
                                 for m in measured),
        "peak_context_used": max(peaks) if peaks else None,
        # The harnesses' own cumulative figures per session, summed; complete only
        # when every measured attempt reported its whole cost.
        "reported_cost_usd": round(sum(costs), 8) if costs else None,
        "cost_complete": bool(measured) and len(measured) == len(attempts) and all(
            isinstance(u, dict) and u.get("cost_complete") for u in usages),
    }


#: Counts a receipt may carry beside ``measure`` (a turn loop's), shown when present.
_COUNTS = ("sessions", "turns", "tool_calls", "rotations", "continuations", "repairs")


def _count(value: Any) -> Any:
    return len(value) if isinstance(value, list) else value


def line(receipt: dict) -> str | None:
    """One compact human line for an attempt; ``None`` when it has no ``measure``.

    Counts come from the receipt's own fields when it has them (``sessions`` may
    be a list), else from ``measure``; an unknown count shows ``?``."""
    measure = receipt.get("measure") if isinstance(receipt, dict) else None
    if not isinstance(measure, dict):
        return None
    prompt = measure.get("prompt") or {}
    parts = [f"prompt ~{prompt.get('est_tokens')} tok ({prompt.get('chars')} chars)"]
    counts = []
    for key in _COUNTS:
        value = _count(receipt[key]) if key in receipt else measure.get(key, "absent")
        if value == "absent":
            continue
        counts.append(f"{key.replace('_', ' ')} {value if value is not None else '?'}")
    parts.append(", ".join(counts))
    usage = measure.get("usage")
    if isinstance(usage, dict):
        peak, size = usage.get("peak_context_used"), usage.get("context_size")
        if peak is not None:
            parts.append(f"peak context {peak}/{size or '?'} ({usage.get('reports')} reports)")
        cost = usage.get("cost_usd")
        parts.append(f"USD {cost} reported" if cost is not None else "cost not reported")
    else:
        parts.append("usage not reported")
    if measure.get("stop_reason"):
        parts.append(f"stop {measure['stop_reason']}")
    if measure.get("approval_denied"):
        parts.append("approval denied")
    if measure.get("liveness"):
        parts.append(f"liveness {measure['liveness']}")
    return "; ".join(parts)


def total_line(total: dict) -> str:
    cost = total.get("reported_cost_usd")
    costs = ("cost not reported" if cost is None
             else f"USD {cost} reported" + ("" if total.get("cost_complete") else " (incomplete)"))
    peak = total.get("peak_context_used")
    return (f"measured {total['measured_attempts']} of {total['attempts']} attempts: "
            f"{total['sessions']} session(s); prompts ~{total['prompt_est_tokens']} tok; "
            f"peak context {peak if peak is not None else '?'}; {costs}")


__all__ = ["ESTIMATE", "VERSION", "activity", "attempt", "combine", "flow_total", "line",
           "prompt_record", "total_line"]
