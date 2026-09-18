"""Paired cost/quality schema for dual-model delegation and runtime routing.

A single-model baseline and a dual-model (or routed) trial run the same task;
this module defines the result record and the comparison. Total-trajectory cost
is the release metric — a reasoning-token drop that raises total spend is a
regression, not a win.

Unknown cost stays unknown: a trial whose spend cannot be priced (e.g. an
external subscription harness with no usage reporting) records ``None`` and is
never compared as zero.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Release thresholds from the approved design. A dual-model rollout passes only
# when all hold on the representative mix documented in
# ``docs/evaluation/dual-model-routing.md``.
MIN_MEDIAN_TOTAL_COST_SAVING = 0.20
MIN_REASONING_INPUT_TOKEN_SAVING = 0.30
MAX_COMPLETION_RATE_REGRESSION = 0.02


@dataclass(frozen=True)
class PairedResult:
    """One trial of one task under one configuration."""

    task_id: str
    trial: str  # "baseline" or "candidate"
    success: bool
    verification_passed: bool | None = None
    total_tokens: int = 0
    total_cost_usd: float | None = None
    reasoning_tokens: int = 0
    reasoning_cost_usd: float | None = None
    collection_tokens: int = 0
    collection_cost_usd: float | None = None
    classifier_tokens: int = 0
    classifier_cost_usd: float | None = None
    wall_ms: int = 0
    model_ms: int = 0
    tool_ms: int = 0
    file_reads: int = 0
    searches: int = 0
    collection_jobs: int = 0
    fallbacks: int = 0
    stale_reports: int = 0
    cost_unknown_reason: str | None = None

    def cost_known(self) -> bool:
        """True when total cost is a priced figure, not an absence."""
        return self.total_cost_usd is not None


@dataclass
class PairedComparison:
    """Baseline vs candidate aggregates over a task mix."""

    tasks: int = 0
    baseline_success: int = 0
    candidate_success: int = 0
    median_total_cost_saving: float | None = None
    median_reasoning_input_saving: float | None = None
    priced_tasks: int = 0
    unpriced_tasks: int = 0
    total_fallbacks: int = 0
    total_stale_reports: int = 0
    notes: list[str] = field(default_factory=list)

    def passes_release_gates(self) -> bool:
        """All design thresholds hold; unpriced tasks never count as savings."""
        if self.priced_tasks == 0:
            return False
        if self.median_total_cost_saving is None or self.median_total_cost_saving < MIN_MEDIAN_TOTAL_COST_SAVING:
            return False
        if (
            self.median_reasoning_input_saving is None
            or self.median_reasoning_input_saving < MIN_REASONING_INPUT_TOKEN_SAVING
        ):
            return False
        if self.tasks == 0:
            return False
        regression = (self.baseline_success - self.candidate_success) / self.tasks
        return regression <= MAX_COMPLETION_RATE_REGRESSION


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


def compare_trials(baselines: list[PairedResult], candidates: list[PairedResult]) -> PairedComparison:
    """Aggregate paired trials by task id.

    Only tasks present in both sets count. Cost medians use priced pairs only;
    tasks with unknown cost are counted separately and contribute no saving.
    """
    by_id = {r.task_id: r for r in baselines}
    comparison = PairedComparison()
    cost_savings: list[float] = []
    reasoning_savings: list[float] = []
    for cand in candidates:
        base = by_id.get(cand.task_id)
        if base is None:
            continue
        comparison.tasks += 1
        comparison.baseline_success += int(base.success)
        comparison.candidate_success += int(cand.success)
        comparison.total_fallbacks += cand.fallbacks
        comparison.total_stale_reports += cand.stale_reports
        if base.total_cost_usd is None or cand.total_cost_usd is None:
            comparison.unpriced_tasks += 1
            reason = cand.cost_unknown_reason or base.cost_unknown_reason or "unpriced model"
            comparison.notes.append(f"{cand.task_id}: cost unknown ({reason}); excluded from savings")
            continue
        comparison.priced_tasks += 1
        if base.total_cost_usd > 0:
            cost_savings.append((base.total_cost_usd - cand.total_cost_usd) / base.total_cost_usd)
        if base.reasoning_tokens > 0:
            reasoning_savings.append((base.reasoning_tokens - cand.reasoning_tokens) / base.reasoning_tokens)
    comparison.median_total_cost_saving = _median(cost_savings)
    comparison.median_reasoning_input_saving = _median(reasoning_savings)
    return comparison


def format_comparison(comparison: PairedComparison) -> str:
    """One human-readable summary block for logs and docs."""

    def pct(value: float | None) -> str:
        return f"{value * 100:.1f}%" if value is not None else "n/a"

    lines = [
        f"tasks={comparison.tasks} priced={comparison.priced_tasks} "
        f"unpriced={comparison.unpriced_tasks}",
        f"success baseline={comparison.baseline_success} candidate={comparison.candidate_success}",
        f"median total-cost saving={pct(comparison.median_total_cost_saving)} "
        f"(gate >= {MIN_MEDIAN_TOTAL_COST_SAVING * 100:.0f}%)",
        f"median reasoning-token saving={pct(comparison.median_reasoning_input_saving)} "
        f"(gate >= {MIN_REASONING_INPUT_TOKEN_SAVING * 100:.0f}%)",
        f"fallbacks={comparison.total_fallbacks} stale={comparison.total_stale_reports}",
        f"release gates={'PASS' if comparison.passes_release_gates() else 'FAIL'}",
    ]
    lines.extend(comparison.notes)
    return "\n".join(lines)
