"""Read-only construction of reproducible dual-model reports from sessions."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from garuda.core.sessions import SessionStore, validate_session_ref
from garuda.eval.dual_model import (
    REPRESENTATIVE_TASK_CATEGORIES,
    TASK_MIX_VERSION,
    PairedComparison,
    PairedResult,
    compare_trials,
    load_task_mix,
    paired_result_from_agent_result,
)
from garuda.types import AgentResult


class PairedReportError(ValueError):
    """A supplied session, manifest, or provenance value cannot support a report."""


@dataclass(frozen=True)
class TrialReference:
    task_id: str
    trial: str
    session_id: str


@dataclass(frozen=True)
class PairedReport:
    trials: tuple[PairedResult, ...]
    comparison: PairedComparison
    payload: dict[str, Any]

    @property
    def release_gates_passed(self) -> bool:
        return self.comparison.passes_release_gates()


def parse_trial_reference(value: str, *, trial: str) -> TrialReference:
    """Parse one ``TASK_ID=SESSION_ID`` value without accepting paths."""
    if trial not in {"baseline", "candidate"}:
        raise PairedReportError(f"unsupported trial kind {trial!r}")
    if not isinstance(value, str) or value.count("=") != 1:
        raise PairedReportError("trial must be TASK_ID=SESSION_ID")
    task_id, session_id = (part.strip() for part in value.split("=", 1))
    if not task_id:
        raise PairedReportError("trial task id must not be empty")
    try:
        validate_session_ref(session_id)
    except ValueError as exc:
        raise PairedReportError(str(exc)) from exc
    return TrialReference(task_id=task_id, trial=trial, session_id=session_id)


def parse_model_versions(values: list[str]) -> dict[str, str]:
    """Parse repeatable ``ROLE=VERSION`` provenance values."""
    versions: dict[str, str] = {}
    for value in values:
        if not isinstance(value, str) or value.count("=") != 1:
            raise PairedReportError("model version must be ROLE=VERSION")
        role, version = (part.strip() for part in value.split("=", 1))
        if not role or not version:
            raise PairedReportError("model version must have a non-empty role and version")
        if role in versions:
            raise PairedReportError(f"model version is duplicated for role {role!r}")
        versions[role] = version
    if "reasoning" not in versions:
        raise PairedReportError("model versions must include a reasoning binding")
    return versions


def load_task_manifest(path: str | Path) -> dict[str, str]:
    """Load exact task-to-category assignments over the required mix."""
    target = Path(path)
    try:
        load_task_mix(target)
    except ValueError as exc:
        raise PairedReportError(str(exc)) from exc
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PairedReportError(f"cannot load task manifest from {target}: {exc}") from exc
    tasks = raw.get("tasks") if isinstance(raw, dict) else None
    if not isinstance(tasks, list) or not tasks:
        raise PairedReportError(f"task manifest at {target} has no tasks list")
    assignments: dict[str, str] = {}
    for task in tasks:
        if not isinstance(task, dict):
            raise PairedReportError(f"task manifest at {target} has a non-object task")
        task_id, category = task.get("id"), task.get("category")
        if not isinstance(task_id, str) or not task_id.strip():
            raise PairedReportError(f"task manifest at {target} has a task without an id")
        if category not in REPRESENTATIVE_TASK_CATEGORIES:
            raise PairedReportError(
                f"task manifest task {task_id!r} has unsupported category {category!r}"
            )
        if task_id in assignments:
            raise PairedReportError(f"task manifest duplicates task id {task_id!r}")
        assignments[task_id] = category
    _validate_task_manifest(assignments)
    return assignments


def _validate_task_manifest(task_manifest: dict[str, str]) -> None:
    """Reject direct service calls that bypass :func:`load_task_manifest`."""
    if not isinstance(task_manifest, dict) or not task_manifest:
        raise PairedReportError("task manifest must assign at least one task")
    for task_id, category in task_manifest.items():
        if not isinstance(task_id, str) or not task_id.strip():
            raise PairedReportError("task manifest has an invalid task id")
        if category not in REPRESENTATIVE_TASK_CATEGORIES:
            raise PairedReportError(
                f"task manifest task {task_id!r} has unsupported category {category!r}"
            )
    missing = REPRESENTATIVE_TASK_CATEGORIES - set(task_manifest.values())
    if missing:
        raise PairedReportError(f"task manifest is missing categories: {sorted(missing)}")


def _validate_evidence_scores(
    evidence_scores: dict[str, dict[str, float]] | None,
) -> dict[str, dict[str, float]]:
    """Validate direct service input just as the JSON loader does."""
    if evidence_scores is None:
        return {}
    if not isinstance(evidence_scores, dict):
        raise PairedReportError("evidence scores must be an object keyed by task id")
    validated: dict[str, dict[str, float]] = {}
    for task_id, row in evidence_scores.items():
        if not isinstance(task_id, str) or not isinstance(row, dict):
            raise PairedReportError("evidence scores must map task ids to score objects")
        parsed: dict[str, float] = {}
        for trial, value in row.items():
            if trial not in {"baseline", "candidate"}:
                raise PairedReportError(f"evidence score for {task_id!r} has unknown trial {trial!r}")
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 1:
                raise PairedReportError(f"evidence score for {task_id!r}/{trial} must be within 0..1")
            parsed[trial] = float(value)
        validated[task_id] = parsed
    return validated


def load_evidence_scores(path: str | Path | None) -> dict[str, dict[str, float]]:
    """Load optional independent score records; absence remains unknown."""
    if path is None:
        return {}
    target = Path(path)
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PairedReportError(f"cannot load evidence scores from {target}: {exc}") from exc
    return _validate_evidence_scores(raw)


def _read_events(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        raise PairedReportError(f"session has no events file at {path}")
    events: list[dict[str, Any]] = []
    try:
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if not line.strip():
                continue
            record = json.loads(line)
            if not isinstance(record, dict):
                raise PairedReportError(f"events file {path} line {line_number} is not an object")
            events.append(record)
    except (OSError, ValueError) as exc:
        if isinstance(exc, PairedReportError):
            raise
        raise PairedReportError(f"cannot parse events file {path}: {exc}") from exc
    if not events:
        raise PairedReportError(f"events file {path} is empty")
    return events


def _result_from_session(
    store: SessionStore,
    reference: TrialReference,
    *,
    evidence_score: float | None,
) -> PairedResult:
    try:
        meta = store.load_meta(reference.session_id)
    except (OSError, ValueError) as exc:
        raise PairedReportError(f"cannot load session {reference.session_id!r}: {exc}") from exc
    if not isinstance(meta, dict):
        raise PairedReportError(f"session {reference.session_id!r} metadata is not an object")
    status = meta.get("status")
    if status not in {"success", "failed"}:
        raise PairedReportError(
            f"session {reference.session_id!r} is not terminal (status={status!r})"
        )
    events = _read_events(store.events_path(reference.session_id))
    turns = meta.get("turns", 0)
    if isinstance(turns, bool) or not isinstance(turns, int) or turns < 0:
        raise PairedReportError(f"session {reference.session_id!r} has invalid turns")
    result = AgentResult(
        success=status == "success",
        final_message="",
        messages=[],
        turns=turns,
        metadata={
            "events": events,
            "metrics": meta.get("metrics") if isinstance(meta.get("metrics"), dict) else {},
            "initial_selection": meta.get("initial_selection"),
        },
    )
    try:
        return paired_result_from_agent_result(
            result,
            task_id=reference.task_id,
            trial=reference.trial,
            evidence_score=evidence_score,
        )
    except ValueError as exc:
        raise PairedReportError(
            f"cannot derive a paired result from session {reference.session_id!r}: {exc}"
        ) from exc


def build_paired_report(
    *,
    store: SessionStore,
    baselines: list[TrialReference],
    candidates: list[TrialReference],
    task_manifest: dict[str, str],
    model_versions: dict[str, str],
    price_source: str,
    prompt_revision: str,
    evidence_scores: dict[str, dict[str, float]] | None = None,
) -> PairedReport:
    """Build a complete paired report without retaining raw session events."""
    if (
        not isinstance(price_source, str)
        or not price_source.strip()
        or not isinstance(prompt_revision, str)
        or not prompt_revision.strip()
    ):
        raise PairedReportError("price source and prompt revision are required")
    if not isinstance(model_versions, dict) or not model_versions.get("reasoning"):
        raise PairedReportError("model versions must include a reasoning binding")
    if any(
        not isinstance(role, str) or not role.strip() or not isinstance(version, str) or not version.strip()
        for role, version in model_versions.items()
    ):
        raise PairedReportError("model versions must map non-empty roles to non-empty versions")
    _validate_task_manifest(task_manifest)
    evidence_scores = _validate_evidence_scores(evidence_scores)
    by_trial = {"baseline": baselines, "candidate": candidates}
    refs: dict[str, dict[str, TrialReference]] = {}
    for trial, entries in by_trial.items():
        for reference in entries:
            if reference.trial != trial:
                raise PairedReportError(
                    f"{trial} input includes a {reference.trial!r} trial reference"
                )
            if not reference.task_id.strip():
                raise PairedReportError("trial task id must not be empty")
            try:
                validate_session_ref(reference.session_id)
            except ValueError as exc:
                raise PairedReportError(str(exc)) from exc
            pair = refs.setdefault(reference.task_id, {})
            if trial in pair:
                raise PairedReportError(f"{trial} is duplicated for task {reference.task_id!r}")
            pair[trial] = reference
    task_ids = set(refs)
    if task_ids != set(task_manifest):
        missing = sorted(set(task_manifest) - task_ids)
        extra = sorted(task_ids - set(task_manifest))
        raise PairedReportError(f"trial tasks do not match manifest (missing={missing}, extra={extra})")
    for task_id, pair in refs.items():
        if set(pair) != {"baseline", "candidate"}:
            raise PairedReportError(f"task {task_id!r} does not have one baseline and one candidate")
    if set(evidence_scores) - task_ids:
        raise PairedReportError("evidence scores name tasks absent from the manifest")

    rows: list[PairedResult] = []
    for task_id in task_manifest:
        for trial in ("baseline", "candidate"):
            row = _result_from_session(
                store,
                refs[task_id][trial],
                evidence_score=evidence_scores.get(task_id, {}).get(trial),
            )
            rows.append(row)
    baseline_rows = [row for row in rows if row.trial == "baseline"]
    candidate_rows = [row for row in rows if row.trial == "candidate"]
    comparison = compare_trials(baseline_rows, candidate_rows)
    payload = {
        "schema_version": 1,
        "task_mix_version": TASK_MIX_VERSION,
        "task_classes": task_manifest,
        "trial_sources": [
            {
                "task_id": task_id,
                "trial": trial,
                "session_id": refs[task_id][trial].session_id,
            }
            for task_id in task_manifest
            for trial in ("baseline", "candidate")
        ],
        "metadata": {
            "model_versions": dict(model_versions),
            "price_source": price_source,
            "prompt_revision": prompt_revision,
        },
        "trials": [row.to_dict() for row in rows],
        "comparison": comparison.to_dict(),
        "release_gates_passed": comparison.passes_release_gates(),
    }
    return PairedReport(trials=tuple(rows), comparison=comparison, payload=payload)


def write_paired_report(path: str | Path, report: PairedReport, *, overwrite: bool = False) -> Path:
    """Write only aggregate report data, refusing accidental replacement."""
    target = Path(path)
    contents = json.dumps(report.payload, indent=2, sort_keys=True) + "\n"
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        if overwrite:
            target.write_text(contents, encoding="utf-8")
        else:
            # Exclusive creation also closes the check-then-write race that an
            # ``exists()`` check would leave open.
            with target.open("x", encoding="utf-8") as handle:
                handle.write(contents)
    except FileExistsError as exc:
        raise PairedReportError(f"refusing to overwrite existing report {target}") from exc
    except OSError as exc:
        raise PairedReportError(f"cannot write paired report to {target}: {exc}") from exc
    return target
