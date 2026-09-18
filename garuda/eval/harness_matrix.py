"""Cross-harness evaluation matrix (P1.12, issue #44).

Compares (model × harness) pairs on capability, completion, cost, latency,
approvals, and handoff success. Three rules are structural, not advisory:

- Unknown cost is `None` and renders as "unknown" — never zero-filled, never
  summed as zero. A total over partially-unknown costs reports the known sum
  *plus* the unknown count.
- Model and harness are separate dimensions. An external harness row carries
  no model claim, and a native row carries no harness claim beyond "native".
- Tasks are reproducible by `(task_id, prompt_hash, harness version)` without
  storing raw task content, and every cell shows its trial count so no vendor
  claim can hide behind a single run.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class HarnessTrial:
    task_id: str
    prompt_hash: str
    model: str | None
    harness: str
    harness_version: str = "unknown"
    capabilities: tuple[str, ...] = ()
    completed: bool | None = None
    cost_usd: float | None = None
    latency_ms: int | None = None
    approvals: int = 0
    handoff: str = "none"
    error: str = ""
    timestamp: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        if not self.task_id:
            raise ValueError("task_id is required")
        if not self.harness:
            raise ValueError("harness is required")
        if self.cost_usd is not None and self.cost_usd < 0:
            raise ValueError("cost_usd cannot be negative")
        if self.handoff not in ("none", "prepared", "acknowledged", "failed"):
            raise ValueError(f"unknown handoff state {self.handoff!r}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "prompt_hash": self.prompt_hash,
            "model": self.model,
            "harness": self.harness,
            "harness_version": self.harness_version,
            "capabilities": list(self.capabilities),
            "completed": self.completed,
            "cost_usd": self.cost_usd,
            "latency_ms": self.latency_ms,
            "approvals": self.approvals,
            "handoff": self.handoff,
            "error": self.error,
            "timestamp": self.timestamp,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "HarnessTrial":
        return cls(
            task_id=data["task_id"],
            prompt_hash=data.get("prompt_hash", ""),
            model=data.get("model"),
            harness=data["harness"],
            harness_version=data.get("harness_version", "unknown"),
            capabilities=tuple(data.get("capabilities", [])),
            completed=data.get("completed"),
            cost_usd=data.get("cost_usd"),
            latency_ms=data.get("latency_ms"),
            approvals=data.get("approvals", 0),
            handoff=data.get("handoff", "none"),
            error=data.get("error", ""),
            timestamp=data.get("timestamp", time.time()),
        )


def prompt_hash(prompt: str) -> str:
    """Reproducibility without content: identify the task, don't store it."""
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:16]


def record_trial(
    task_id: str,
    prompt: str,
    *,
    model: str | None,
    harness: str,
    **fields: Any,
) -> HarnessTrial:
    """Build a trial from a raw prompt (hashed, never stored)."""
    return HarnessTrial(
        task_id=task_id, prompt_hash=prompt_hash(prompt),
        model=model, harness=harness, **fields,
    )


@dataclass
class MatrixCell:
    model: str | None
    harness: str
    trials: int = 0
    completed: int = 0
    known_cost_usd: float = 0.0
    unknown_costs: int = 0
    latency_ms: list[int] = field(default_factory=list)
    approvals: int = 0
    handoffs_acknowledged: int = 0
    handoffs_attempted: int = 0

    def to_dict(self) -> dict[str, Any]:
        latencies = sorted(self.latency_ms)
        median = latencies[len(latencies) // 2] if latencies else None
        return {
            "model": self.model,
            "harness": self.harness,
            "trials": self.trials,
            "completed": self.completed,
            "completion_rate": (self.completed / self.trials) if self.trials else None,
            "cost_usd_known_sum": round(self.known_cost_usd, 6),
            "cost_unknown": self.unknown_costs,
            "latency_ms_median": median,
            "latency_unknown": self.trials - len(latencies),
            "approvals": self.approvals,
            "handoffs_acknowledged": self.handoffs_acknowledged,
            "handoffs_attempted": self.handoffs_attempted,
        }


def summarize(trials: list[HarnessTrial]) -> list[MatrixCell]:
    """Aggregate by (model, harness). Unknowns stay unknown at every step."""
    cells: dict[tuple[str | None, str], MatrixCell] = {}
    for trial in trials:
        key = (trial.model, trial.harness)
        cell = cells.get(key)
        if cell is None:
            cell = cells[key] = MatrixCell(model=trial.model, harness=trial.harness)
        cell.trials += 1
        if trial.completed:
            cell.completed += 1
        if trial.cost_usd is None:
            cell.unknown_costs += 1
        else:
            cell.known_cost_usd += trial.cost_usd
        if trial.latency_ms is not None:
            cell.latency_ms.append(trial.latency_ms)
        cell.approvals += trial.approvals
        if trial.handoff != "none":
            cell.handoffs_attempted += 1
            if trial.handoff == "acknowledged":
                cell.handoffs_acknowledged += 1
    return [cells[key] for key in sorted(cells, key=lambda k: (k[1], k[0] or ""))]


def render_table(cells: list[MatrixCell]) -> str:
    """Markdown matrix. Single-trial cells read as n=1, never as verdicts."""
    lines = [
        "| Model | Harness | n | Done | Cost (known + unknown) | Latency med | Approvals | Handoff |",
        "|-------|---------|---|------|------------------------|-------------|-----------|---------|",
    ]
    for cell in cells:
        data = cell.to_dict()
        rate = data["completion_rate"]
        done = f"{data['completed']}/{data['trials']}"
        done += f" ({rate*100:.0f}%)" if rate is not None else ""
        cost = f"${data['cost_usd_known_sum']:.4f} + {data['cost_unknown']} unknown"
        latency = (
            f"{data['latency_ms_median']}ms" if data["latency_ms_median"] is not None else "unknown"
        )
        handoff = f"{data['handoffs_acknowledged']}/{data['handoffs_attempted']}"
        lines.append(
            f"| {cell.model or '—'} | {cell.harness} | {data['trials']} | {done} "
            f"| {cost} | {latency} | {data['approvals']} | {handoff} |"
        )
    return "\n".join(lines)
