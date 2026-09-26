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
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
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
        if not isinstance(self.task_id, str) or not self.task_id:
            raise ValueError("task_id is required")
        if not isinstance(self.prompt_hash, str) or not self.prompt_hash:
            raise ValueError("prompt_hash must be a non-empty string")
        if not re.fullmatch(r"[0-9a-f]+", self.prompt_hash):
            raise ValueError("prompt_hash must be hex")
        if not isinstance(self.harness, str) or not self.harness:
            raise ValueError("harness is required")
        if not isinstance(self.harness_version, str) or not self.harness_version:
            raise ValueError("harness_version must be a non-empty string")
        if any(not isinstance(capability, str) or not capability for capability in self.capabilities):
            raise ValueError("capabilities must contain non-empty strings")
        if self.model is not None and (not isinstance(self.model, str) or not self.model):
            raise ValueError("model must be a non-empty string or null")
        if self.completed is not None and not isinstance(self.completed, bool):
            raise ValueError("completed must be a boolean or null")
        if self.cost_usd is not None and (
            not isinstance(self.cost_usd, (int, float))
            or isinstance(self.cost_usd, bool)
            or self.cost_usd < 0
        ):
            raise ValueError("cost_usd cannot be negative")
        if self.latency_ms is not None and (
            not isinstance(self.latency_ms, int) or isinstance(self.latency_ms, bool)
            or self.latency_ms < 0
        ):
            raise ValueError("latency_ms must be a non-negative integer or null")
        if not isinstance(self.approvals, int) or isinstance(self.approvals, bool) or self.approvals < 0:
            raise ValueError("approvals must be a non-negative integer")
        if not isinstance(self.handoff, str) or self.handoff not in (
            "none", "prepared", "acknowledged", "failed"
        ):
            raise ValueError(f"unknown handoff state {self.handoff!r}")
        if not isinstance(self.error, str):
            raise ValueError("error must be a string")
        if not isinstance(self.timestamp, (int, float)) or isinstance(self.timestamp, bool):
            raise ValueError("timestamp must be a number")

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
        """Validated ingestion. Wrong shapes fail closed with a field path —
        a summed string cost or a counted `"yes"` completion must never reach
        aggregation silently."""
        if not isinstance(data, dict):
            raise ValueError("trial must be a mapping")
        try:
            task_id = data["task_id"]
            harness = data["harness"]
        except KeyError as exc:
            raise ValueError(f"trial missing required field {exc}") from exc
        prompt_hash_value = data.get("prompt_hash", "")
        if (
            not isinstance(prompt_hash_value, str)
            or not prompt_hash_value
        ):
            raise ValueError("trial.prompt_hash must be a non-empty string")
        if not re.fullmatch(r"[0-9a-f]+", prompt_hash_value):
            raise ValueError("trial.prompt_hash must be hex")
        model = data.get("model")
        if model is not None and (not isinstance(model, str) or not model):
            raise ValueError("trial.model must be a non-empty string or null")
        completed = data.get("completed")
        if completed is not None and not isinstance(completed, bool):
            raise ValueError("trial.completed must be a boolean or null")
        cost = data.get("cost_usd")
        if cost is not None and (not isinstance(cost, (int, float)) or isinstance(cost, bool)):
            raise ValueError("trial.cost_usd must be a number or null")
        latency = data.get("latency_ms")
        if latency is not None and (not isinstance(latency, int) or isinstance(latency, bool)):
            raise ValueError("trial.latency_ms must be an integer or null")
        if latency is not None and latency < 0:
            raise ValueError("trial.latency_ms cannot be negative")
        approvals = data.get("approvals", 0)
        if not isinstance(approvals, int) or isinstance(approvals, bool) or approvals < 0:
            raise ValueError("trial.approvals must be a non-negative integer")
        error = data.get("error", "")
        if not isinstance(error, str):
            raise ValueError("trial.error must be a string")
        capabilities = data.get("capabilities", [])
        if not isinstance(capabilities, list) or any(
            not isinstance(c, str) for c in capabilities
        ):
            raise ValueError("trial.capabilities must be a list of strings")
        timestamp = data.get("timestamp", time.time())
        if not isinstance(timestamp, (int, float)) or isinstance(timestamp, bool):
            raise ValueError("trial.timestamp must be a number")
        return cls(
            task_id=task_id,
            prompt_hash=prompt_hash_value,
            model=model,
            harness=harness,
            harness_version=data.get("harness_version", "unknown"),
            capabilities=tuple(capabilities),
            completed=completed,
            cost_usd=cost,
            latency_ms=latency,
            approvals=approvals,
            handoff=data.get("handoff", "none"),
            error=error,
            timestamp=timestamp,
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


def load_trials(path: str) -> list[HarnessTrial]:
    """Load an external harness result feed through the same validation path."""
    source = json.loads(Path(path).read_text(encoding="utf-8"))
    raw = source.get("trials") if isinstance(source, dict) else source
    if not isinstance(raw, list):
        raise ValueError("trial feed must be a list or an object with a trials list")
    return [HarnessTrial.from_dict(item) for item in raw]


def write_matrix(path: str, trials: list[HarnessTrial]) -> list[MatrixCell]:
    """Persist the measured trials and rendered cells as one report artifact."""
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    cells = summarize(trials)
    destination.write_text(
        json.dumps(
            {
                "trials": [trial.to_dict() for trial in trials],
                "cells": [cell.to_dict() for cell in cells],
                "markdown": render_table(cells),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return cells


@dataclass
class MatrixCell:
    model: str | None
    harness: str
    trials: int = 0
    completed: int = 0
    unknown_completions: int = 0
    known_cost_usd: float = 0.0
    unknown_costs: int = 0
    latency_ms: list[int] = field(default_factory=list)
    approvals: int = 0
    handoffs_acknowledged: int = 0
    handoffs_attempted: int = 0

    def to_dict(self) -> dict[str, Any]:
        latencies = sorted(self.latency_ms)
        median: float | int | None = None
        if latencies:
            mid = len(latencies) // 2
            if len(latencies) % 2:
                median = latencies[mid]
            else:
                median = (latencies[mid - 1] + latencies[mid]) / 2
        known = self.trials - self.unknown_completions
        return {
            "model": self.model,
            "harness": self.harness,
            "trials": self.trials,
            "completed": self.completed,
            "unknown_completions": self.unknown_completions,
            "completion_rate": (self.completed / known) if known else None,
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
        if trial.completed is None:
            cell.unknown_completions += 1
        elif trial.completed:
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
        if data["unknown_completions"]:
            done += f" +{data['unknown_completions']} unknown"
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


def trials_from_ablation(
    tasks, results, *, model: str | None, harness: str = "native"
) -> list[HarnessTrial]:
    """Record real eval runs into matrix trials.

    Feeds `run_ablation` output back into the comparison layer: prompt text
    is hashed (never stored), ungraded runs stay `completed=None` (unknown,
    never failure), and costs stay unknown unless a pricier pipeline fills
    them — the product comparison is populated by measured runs, not fixtures.
    """
    prompts = {t.id: t.prompt for t in tasks}
    trials = []
    for result in results:
        if result.task_id not in prompts:
            raise ValueError(f"ablation result references unknown task {result.task_id!r}")
        prompt = prompts[result.task_id]
        if result.graded_pass is not None:
            completed: bool | None = result.graded_pass
        elif result.error or not result.agent_success:
            completed = False
        else:
            completed = None
        trials.append(
            record_trial(
                result.task_id,
                prompt,
                model=model,
                harness=harness,
                harness_version="native" if harness == "native" else "unknown",
                capabilities=("prompt", "cancel") if harness == "native" else (),
                completed=completed,
                latency_ms=result.duration_ms,
                error=result.error or "",
            )
        )
    return trials
