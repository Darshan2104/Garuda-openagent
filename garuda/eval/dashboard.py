"""Cost / latency dashboard over persisted sessions and ATIF trajectories.

Aggregates token usage, estimated cost, turns, and wall-clock duration across
runs so you can see where tokens and money go. Two sources:

* the session store (``~/.garuda/sessions`` or ``GARUDA_SESSIONS_DIR``) — every
  ``garuda run`` persists there, and ``meta.json`` carries usage after finish;
* explicit ATIF trajectory JSON files passed as arguments.

    python -m garuda.eval.dashboard                 # all recent sessions
    python -m garuda.eval.dashboard run1.json run2.json
"""

import argparse
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from garuda.core.sessions import SessionStore
from garuda.eval.costs import duration_ms, estimate_cost


@dataclass
class RunRow:
    source: str
    model: str | None
    status: str
    turns: int | None
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    cost_usd: float | None
    duration_ms: int | None
    extra: dict[str, Any] = field(default_factory=dict)


def _short(value: str, width: int) -> str:
    value = value or ""
    return value if len(value) <= width else value[: width - 1] + "…"


def row_from_session_meta(meta: dict[str, Any]) -> RunRow:
    usage = meta.get("usage") or {}
    prompt = int(usage.get("prompt_tokens", 0) or 0)
    completion = int(usage.get("completion_tokens", 0) or 0)
    total = int(usage.get("total_tokens", 0) or (prompt + completion))
    model = meta.get("model")
    cost = estimate_cost(model, usage)
    return RunRow(
        source=meta.get("session_id", "?")[:8],
        model=model,
        status=meta.get("status", "?"),
        turns=meta.get("turns"),
        prompt_tokens=prompt,
        completion_tokens=completion,
        total_tokens=total,
        cost_usd=cost,
        duration_ms=duration_ms(meta.get("created_at"), meta.get("updated_at")),
    )


def row_from_atif(path: Path) -> RunRow:
    data = json.loads(path.read_text(encoding="utf-8"))
    metrics = data.get("final_metrics", {})
    extra = metrics.get("extra") or {}
    prompt = int(metrics.get("total_prompt_tokens", 0) or 0)
    completion = int(metrics.get("total_completion_tokens", 0) or 0)
    total = int(extra.get("total_tokens", 0) or (prompt + completion))
    return RunRow(
        source=path.name,
        model=(data.get("agent") or {}).get("model_name"),
        status="success" if extra.get("success") else ("failed" if "success" in extra else "?"),
        turns=extra.get("turns"),
        prompt_tokens=prompt,
        completion_tokens=completion,
        total_tokens=total,
        cost_usd=metrics.get("total_cost_usd"),
        duration_ms=extra.get("duration_ms"),
    )


def collect_rows(
    store: SessionStore | None = None, atif_files: list[Path] | None = None, limit: int = 50
) -> list[RunRow]:
    rows: list[RunRow] = []
    for path in atif_files or []:
        try:
            rows.append(row_from_atif(path))
        except (OSError, json.JSONDecodeError, KeyError):
            continue
    if not atif_files:
        store = store or SessionStore()
        for meta in store.list_sessions(limit=limit):
            rows.append(row_from_session_meta(meta))
    return rows


def _fmt_cost(cost: float | None) -> str:
    return f"${cost:.4f}" if cost is not None else "—"


def _fmt_dur(ms: int | None) -> str:
    if ms is None:
        return "—"
    return f"{ms/1000:.1f}s" if ms < 60_000 else f"{ms/60_000:.1f}m"


def render_dashboard(rows: list[RunRow]) -> str:
    if not rows:
        return "No runs found. Run a task first (garuda run -t ...) or pass ATIF files."
    header = (
        "| Run | Model | Status | Turns | Prompt | Compl. | Total | Cost | Duration |\n"
        "|-----|-------|--------|-------|--------|--------|-------|------|----------|"
    )
    lines = [header]
    tot_prompt = tot_completion = tot_total = 0
    tot_cost = 0.0
    any_cost = False
    for r in rows:
        lines.append(
            f"| {_short(r.source, 12)} | {_short(r.model or '—', 24)} | {r.status} "
            f"| {r.turns if r.turns is not None else '—'} | {r.prompt_tokens} "
            f"| {r.completion_tokens} | {r.total_tokens} | {_fmt_cost(r.cost_usd)} "
            f"| {_fmt_dur(r.duration_ms)} |"
        )
        tot_prompt += r.prompt_tokens
        tot_completion += r.completion_tokens
        tot_total += r.total_tokens
        if r.cost_usd is not None:
            tot_cost += r.cost_usd
            any_cost = True
    lines.append(
        f"| **TOTAL ({len(rows)})** |  |  |  | {tot_prompt} | {tot_completion} | {tot_total} "
        f"| {_fmt_cost(tot_cost) if any_cost else '—'} |  |"
    )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(prog="garuda-dashboard", description="Cost/latency dashboard")
    parser.add_argument("atif_files", nargs="*", help="Optional ATIF trajectory JSON files")
    parser.add_argument("--limit", type=int, default=50, help="Max sessions to show")
    parser.add_argument("--sessions-dir", help="Override the session store root")
    args = parser.parse_args()

    store = SessionStore(root=args.sessions_dir) if args.sessions_dir else SessionStore()
    atif = [Path(p) for p in args.atif_files]
    rows = collect_rows(store=store, atif_files=atif, limit=args.limit)
    print(render_dashboard(rows))


if __name__ == "__main__":
    main()
