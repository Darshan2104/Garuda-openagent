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

from garuda.eval.costs import duration_ms, estimate_cost
from garuda.types import AgentResult

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


def classifier_accounting(record: dict[str, Any] | None) -> tuple[int, float | None]:
    """``(classifier_tokens, classifier_cost_usd)`` from a persisted selection.

    Accepts session meta or its ``initial_selection`` record (issue #80).
    A session whose classifier made no call contributes a known zero; a call
    whose cost could not be priced contributes ``None`` so the trial total
    stays unknown instead of silently dropping the classifier's spend.
    """
    if not isinstance(record, dict):
        return 0, 0.0
    selection = record.get("initial_selection", record)
    slot = selection.get("classifier") if isinstance(selection, dict) else None
    if not isinstance(slot, dict) or not slot.get("evaluated"):
        return 0, 0.0
    usage = slot.get("usage") or {}
    tokens = 0
    for key in ("prompt", "completion"):
        value = usage.get(key) if isinstance(usage, dict) else None
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            tokens += value
    cost = slot.get("cost_usd")
    if isinstance(cost, bool) or not isinstance(cost, (int, float)) or cost < 0:
        return tokens, None
    return tokens, float(cost)


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


_FILE_READ_TOOLS = frozenset(
    {
        "read_file",
        "grep",
        "glob",
        "ls",
        "read_pdf",
        "read_spreadsheet",
        "image_read",
    }
)
_SEARCH_TOOLS = frozenset({"web_fetch", "web_search"})


def _token_total(usage: object) -> int:
    if not isinstance(usage, dict):
        return 0
    total = usage.get("total_tokens")
    if isinstance(total, (int, float)) and not isinstance(total, bool):
        return max(0, int(total))
    return max(0, int(usage.get("prompt_tokens", 0) or 0)) + max(
        0, int(usage.get("completion_tokens", 0) or 0)
    )


def _input_tokens(usage: object) -> int:
    if not isinstance(usage, dict):
        return 0
    return max(0, int(usage.get("prompt_tokens", 0) or 0))


def _cost(model: object, usage: object, explicit: object = None) -> float | None:
    """One call's cost, preserving unavailable pricing as ``None``."""
    if isinstance(explicit, (int, float)) and not isinstance(explicit, bool) and explicit >= 0:
        return float(explicit)
    return estimate_cost(str(model) if model else None, usage if isinstance(usage, dict) else None)


def paired_result_from_agent_result(
    result: AgentResult,
    *,
    task_id: str,
    trial: str,
    initial_selection: dict[str, Any] | None = None,
    evidence_score: float | None = None,
) -> PairedResult:
    """Convert one native run into a lossless paired-evaluation trial record.

    Parent model responses live in ``AgentResult.metadata['events']``. Collection
    children retain separate trails, so the coordinator's terminal lifecycle event
    carries an ``attempt_metrics`` summary; consuming both sources avoids silently
    dropping child spend or making a fallback look free. This function does not
    invent quality scores: callers supply an independently graded score when one
    exists, otherwise it remains ``None`` in the report.
    """
    metadata = result.metadata if isinstance(result.metadata, dict) else {}
    events = metadata.get("events") if isinstance(metadata.get("events"), list) else []
    calls: list[dict[str, Any]] = []
    collection_jobs = fallbacks = stale_reports = 0
    file_reads = searches = 0
    verification: bool | None = None
    timestamps: list[str] = []

    for event in events:
        if not isinstance(event, dict):
            continue
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        timestamp = event.get("timestamp")
        if isinstance(timestamp, str):
            timestamps.append(timestamp)
        event_type = event.get("type")
        if event_type == "model_response" and not payload.get("truncated"):
            calls.append(
                {
                    "role": payload.get("model_binding_role"),
                    "purpose": payload.get("call_purpose"),
                    "model": payload.get("model"),
                    "usage": payload.get("usage") or {},
                    "cost": None,
                    "duration_ms": payload.get("duration_ms", 0),
                }
            )
        elif event_type == "tool_call":
            name = payload.get("name")
            if name in _FILE_READ_TOOLS:
                file_reads += 1
            if name in _SEARCH_TOOLS:
                searches += 1
        elif event_type == "verification" and isinstance(payload.get("approved"), bool):
            verification = payload["approved"]
        elif event_type == "model_fallback":
            fallbacks += 1
        elif event_type == "collection":
            state = payload.get("state")
            if state in {"completed", "completed_stale"}:
                collection_jobs += 1
            if state == "completed_stale":
                stale_reports += 1
            for attempt in payload.get("attempt_metrics", []):
                if not isinstance(attempt, dict):
                    continue
                calls.append(
                    {
                        "role": attempt.get("model_binding_role"),
                        "purpose": attempt.get("call_purpose"),
                        "model": attempt.get("model"),
                        "usage": attempt.get("usage") or {},
                        "cost": attempt.get("cost_usd"),
                        "duration_ms": attempt.get("elapsed_ms", 0),
                    }
                )

    by_role: dict[str, dict[str, Any]] = {
        "reasoning": {"tokens": 0, "input": 0, "costs": [], "calls": 0},
        "collection": {"tokens": 0, "input": 0, "costs": [], "calls": 0},
    }
    unattributed = 0
    model_ms = 0
    total_call_tokens = 0
    total_call_costs: list[float | None] = []
    for call in calls:
        usage = call["usage"]
        cost = _cost(call["model"], usage, call["cost"])
        total_call_tokens += _token_total(usage)
        total_call_costs.append(cost)
        try:
            model_ms += max(0, int(call["duration_ms"] or 0))
        except (TypeError, ValueError):
            pass
        role = call["role"]
        if role not in by_role or not call["purpose"]:
            unattributed += 1
            continue
        record = by_role[role]
        record["tokens"] += _token_total(usage)
        record["input"] += _input_tokens(usage)
        record["calls"] += 1
        record["costs"].append(cost)

    if initial_selection is None:
        initial_selection = metadata.get("initial_selection")
    classifier_tokens, classifier_cost = classifier_accounting(initial_selection)
    role_costs = {
        role: aggregate_role_costs(record["costs"]) if record["calls"] else 0.0
        for role, record in by_role.items()
    }
    total_cost = aggregate_role_costs([*total_call_costs, classifier_cost])
    total_tokens = total_call_tokens + classifier_tokens
    metrics = metadata.get("metrics") if isinstance(metadata.get("metrics"), dict) else {}
    tool_ms = int(metrics.get("tool_ms_total", 0) or 0)
    if timestamps:
        wall = duration_ms(min(timestamps), max(timestamps))
    else:
        wall = None
    cost_unknown_reason = None
    if total_cost is None:
        cost_unknown_reason = "one or more model calls reported usage without a price"
    return PairedResult(
        task_id=task_id,
        trial=trial,
        success=result.success,
        verification_passed=verification,
        total_tokens=total_tokens,
        total_cost_usd=total_cost,
        reasoning_tokens=by_role["reasoning"]["tokens"],
        reasoning_cost_usd=role_costs["reasoning"],
        collection_tokens=by_role["collection"]["tokens"],
        collection_cost_usd=role_costs["collection"],
        classifier_tokens=classifier_tokens,
        classifier_cost_usd=classifier_cost,
        wall_ms=wall if wall is not None else model_ms + tool_ms,
        model_ms=model_ms,
        tool_ms=tool_ms,
        file_reads=file_reads,
        searches=searches,
        collection_jobs=collection_jobs,
        fallbacks=fallbacks,
        stale_reports=stale_reports,
        # The collection runtime has no mutating capability. This is still
        # reported as a distinct release gate rather than inferred from success.
        collection_mutations=0,
        reasoning_input_tokens=by_role["reasoning"]["input"],
        evidence_score=evidence_score,
        attribution_complete=(bool(calls) and unattributed == 0) if calls else None,
        cost_unknown_reason=cost_unknown_reason,
    )


def save_paired_results(
    path: str | Path,
    results: Iterable[PairedResult],
    *,
    metadata: dict[str, Any],
) -> Path:
    """Persist reproducible paired-run evidence without overwriting its context.

    The caller records immutable model versions, price source, prompt revision,
    seed support, and command/environment evidence in ``metadata``. The writer
    refuses an unlabelled report, since bare result rows cannot substantiate a
    rollout decision.
    """
    required = {"model_versions", "price_source", "prompt_revision"}
    missing = sorted(key for key in required if not metadata.get(key))
    if missing:
        raise ValueError(f"paired report metadata is missing: {missing}")
    target = Path(path)
    payload = {
        "schema_version": 1,
        "task_mix_version": TASK_MIX_VERSION,
        "metadata": metadata,
        "trials": [result.to_dict() for result in results],
    }
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return target
