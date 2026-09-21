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

import json
from collections.abc import Iterable
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

# Release thresholds from the approved design. A dual-model rollout passes only
# when all hold on the representative mix documented in
# ``docs/evaluation/dual-model-routing.md``.
MIN_MEDIAN_TOTAL_COST_SAVING = 0.20
MIN_REASONING_INPUT_TOKEN_SAVING = 0.30
MAX_COMPLETION_RATE_REGRESSION = 0.02
# Prior prompt-only changes reduced repository exploration while tests still
# passed, so the gate also watches investigation volume and evidence quality.
# A candidate may run at most 10% fewer investigation calls than the baseline,
# and mean evidence score may drop at most 0.05 absolute, before the gate
# fails pending analysis. Tolerances, not guarantees: missing one keeps
# collection opt-in, it never suppresses the underlying counts.
MAX_INVESTIGATION_DECLINE = 0.10
MAX_EVIDENCE_SCORE_DECLINE = 0.05

# Representative task mix for the paired trials (issue #81 scope). Pinned here
# so reports can cite the mix version; the definition itself lives in the
# offline fixture beside this module.
TASK_MIX_VERSION = "2026-09-21"
TASK_MIX_FILENAME = "dual_model_task_mix.json"
PAIRED_REPORT_FILENAME = "dual_model_paired_report.json"
REPRESENTATIVE_TASK_CATEGORIES = frozenset(
    {"read-heavy", "debugging", "implementation", "doc-analysis", "do-not-delegate"}
)


def fixtures_dir() -> Path:
    """Directory holding the offline dual-model fixtures next to this module."""
    return Path(__file__).resolve().parent / "fixtures"


def load_task_mix(path: str | Path | None = None) -> dict[str, Any]:
    """Load and validate the representative task-mix definition.

    Fail closed: a mix missing a required category, or a category without a
    boolean ``delegation_expected`` flag, is rejected rather than silently
    narrowed — a narrowed mix could hide over-delegation.
    """
    target = Path(path) if path is not None else fixtures_dir() / TASK_MIX_FILENAME
    try:
        mix = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"cannot load dual-model task mix from {target}: {exc}") from exc
    categories = mix.get("categories") if isinstance(mix, dict) else None
    if not isinstance(categories, list) or not categories:
        raise ValueError(f"task mix at {target} has no categories list")
    seen: set[str] = set()
    for entry in categories:
        if not isinstance(entry, dict) or not entry.get("id"):
            raise ValueError(f"task mix at {target} has a category without an id: {entry!r}")
        if not isinstance(entry.get("delegation_expected"), bool):
            raise ValueError(
                f"task mix category {entry.get('id')!r} must declare "
                "delegation_expected as a boolean"
            )
        seen.add(entry["id"])
    missing = REPRESENTATIVE_TASK_CATEGORIES - seen
    if missing:
        raise ValueError(f"task mix at {target} is missing categories: {sorted(missing)}")
    return mix


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
    collection_mutations: int = 0
    reasoning_input_tokens: int | None = None
    evidence_score: float | None = None
    attribution_complete: bool | None = None
    cost_unknown_reason: str | None = None

    def __post_init__(self) -> None:
        if self.evidence_score is not None and not 0.0 <= self.evidence_score <= 1.0:
            raise ValueError(f"evidence_score must be within 0..1, got {self.evidence_score!r}")

    def cost_known(self) -> bool:
        """True when total cost is a priced figure, not an absence."""
        return self.total_cost_usd is not None

    @property
    def investigation_calls(self) -> int:
        """Repository-investigation volume: file reads plus searches."""
        return self.file_reads + self.searches

    def reasoning_input(self) -> int:
        """Reasoning-model input tokens: the release-metric input.

        Falls back to ``reasoning_tokens`` when the input split was not
        recorded separately, so older records keep their previous meaning.
        """
        if self.reasoning_input_tokens is not None:
            return self.reasoning_input_tokens
        return self.reasoning_tokens

    def to_dict(self) -> dict[str, Any]:
        """Plain-dict form for offline fixture reports."""
        return {f.name: getattr(self, f.name) for f in fields(self)}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PairedResult":
        """Rebuild a trial from a fixture dict; unknown keys are ignored so a
        newer report still reads under an older schema."""
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})


def aggregate_role_costs(costs: Iterable[float | None]) -> float | None:
    """Sum per-role costs with unknown propagation.

    Any unpriced role makes the total unknown (``None``): a partial sum is
    never reported as the trajectory cost, and a missing external subscription
    cost is never compared as zero.
    """
    total = 0.0
    for cost in costs:
        if cost is None:
            return None
        total += cost
    return round(total, 8)


def role_cost_total(result: PairedResult) -> float | None:
    """Per-role cost aggregate for one trial: reasoning + collection + classifier."""
    return aggregate_role_costs(
        [result.reasoning_cost_usd, result.collection_cost_usd, result.classifier_cost_usd]
    )


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
    baseline_investigation: int = 0
    candidate_investigation: int = 0
    baseline_evidence_avg: float | None = None
    candidate_evidence_avg: float | None = None
    attribution_gaps: int = 0
    attribution_unknown: int = 0
    collection_mutations: int = 0
    notes: list[str] = field(default_factory=list)

    def fallback_rate(self) -> float | None:
        """Share of paired tasks whose candidate trial fell back, if any tasks paired."""
        return self.total_fallbacks / self.tasks if self.tasks else None

    def stale_rate(self) -> float | None:
        """Share of paired tasks whose candidate trial reported stale output."""
        return self.total_stale_reports / self.tasks if self.tasks else None

    def investigation_ratio(self) -> float | None:
        """Candidate over baseline investigation calls; None when the baseline ran none."""
        if self.baseline_investigation <= 0:
            return None
        return self.candidate_investigation / self.baseline_investigation

    def passes_release_gates(self) -> bool:
        """All design thresholds hold; unpriced tasks never count as savings.

        Signals that were never recorded (no evidence scores, no attribution
        attestation, no baseline investigation) do not fail the gate on their
        own — they are reported in the aggregates and notes instead. Explicit
        violations do fail: unattributed candidate calls, any
        collection-authorized mutation, a steep investigation drop, or an
        evidence-score drop beyond tolerance.
        """
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
        if regression > MAX_COMPLETION_RATE_REGRESSION:
            return False
        if self.collection_mutations > 0:
            return False
        if self.attribution_gaps > 0:
            return False
        ratio = self.investigation_ratio()
        if ratio is not None and ratio < 1.0 - MAX_INVESTIGATION_DECLINE:
            return False
        if (
            self.baseline_evidence_avg is not None
            and self.candidate_evidence_avg is not None
            and self.candidate_evidence_avg
            < self.baseline_evidence_avg - MAX_EVIDENCE_SCORE_DECLINE
        ):
            return False
        return True

    def to_dict(self) -> dict[str, Any]:
        """Plain-dict form for offline fixture reports."""
        return {f.name: getattr(self, f.name) for f in fields(self)}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PairedComparison":
        """Rebuild an aggregate from a fixture dict; unknown keys are ignored."""
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})


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
    evidence_base: list[float] = []
    evidence_cand: list[float] = []
    for cand in candidates:
        base = by_id.get(cand.task_id)
        if base is None:
            continue
        comparison.tasks += 1
        comparison.baseline_success += int(base.success)
        comparison.candidate_success += int(cand.success)
        comparison.total_fallbacks += cand.fallbacks
        comparison.total_stale_reports += cand.stale_reports
        comparison.baseline_investigation += base.investigation_calls
        comparison.candidate_investigation += cand.investigation_calls
        comparison.collection_mutations += cand.collection_mutations
        if cand.attribution_complete is False:
            comparison.attribution_gaps += 1
        elif cand.attribution_complete is None:
            comparison.attribution_unknown += 1
        if base.evidence_score is not None and cand.evidence_score is not None:
            evidence_base.append(base.evidence_score)
            evidence_cand.append(cand.evidence_score)
        if base.total_cost_usd is None or cand.total_cost_usd is None:
            comparison.unpriced_tasks += 1
            reason = cand.cost_unknown_reason or base.cost_unknown_reason or "unpriced model"
            comparison.notes.append(f"{cand.task_id}: cost unknown ({reason}); excluded from savings")
            continue
        comparison.priced_tasks += 1
        if base.total_cost_usd > 0:
            cost_savings.append((base.total_cost_usd - cand.total_cost_usd) / base.total_cost_usd)
        base_input = base.reasoning_input()
        if base_input > 0:
            reasoning_savings.append((base_input - cand.reasoning_input()) / base_input)
    comparison.median_total_cost_saving = _median(cost_savings)
    comparison.median_reasoning_input_saving = _median(reasoning_savings)
    if evidence_base:
        comparison.baseline_evidence_avg = sum(evidence_base) / len(evidence_base)
        comparison.candidate_evidence_avg = sum(evidence_cand) / len(evidence_cand)
    if comparison.attribution_unknown:
        comparison.notes.append(
            f"{comparison.attribution_unknown} candidate trial(s) lack role×purpose "
            "attestation; counted here, not treated as savings or as gaps"
        )
    return comparison


def format_comparison(comparison: PairedComparison) -> str:
    """One human-readable summary block for logs and docs."""

    def pct(value: float | None) -> str:
        return f"{value * 100:.1f}%" if value is not None else "n/a"

    def avg(value: float | None) -> str:
        return f"{value:.3f}" if value is not None else "n/a"

    lines = [
        f"tasks={comparison.tasks} priced={comparison.priced_tasks} "
        f"unpriced={comparison.unpriced_tasks}",
        f"success baseline={comparison.baseline_success} candidate={comparison.candidate_success}",
        f"median total-cost saving={pct(comparison.median_total_cost_saving)} "
        f"(gate >= {MIN_MEDIAN_TOTAL_COST_SAVING * 100:.0f}%)",
        f"median reasoning-input saving={pct(comparison.median_reasoning_input_saving)} "
        f"(gate >= {MIN_REASONING_INPUT_TOKEN_SAVING * 100:.0f}%)",
        f"investigation baseline={comparison.baseline_investigation} "
        f"candidate={comparison.candidate_investigation} "
        f"(ratio={pct(comparison.investigation_ratio())})",
        f"evidence baseline={avg(comparison.baseline_evidence_avg)} "
        f"candidate={avg(comparison.candidate_evidence_avg)}",
        f"attribution gaps={comparison.attribution_gaps} "
        f"unknown={comparison.attribution_unknown} "
        f"collection_mutations={comparison.collection_mutations}",
        f"fallbacks={comparison.total_fallbacks} "
        f"(rate={pct(comparison.fallback_rate())}) "
        f"stale={comparison.total_stale_reports} "
        f"(rate={pct(comparison.stale_rate())})",
        f"release gates={'PASS' if comparison.passes_release_gates() else 'FAIL'}",
    ]
    lines.extend(comparison.notes)
    return "\n".join(lines)
