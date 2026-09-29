"""Paired-report construction reads completed sessions without copying transcripts."""

import json

import pytest

from garuda.core.sessions import SessionStore
from garuda.eval.paired_report import (
    PairedReportError,
    TrialReference,
    build_paired_report,
    load_task_manifest,
    parse_trial_reference,
    write_paired_report,
)

_CATEGORIES = (
    "read-heavy",
    "debugging",
    "implementation",
    "doc-analysis",
    "do-not-delegate",
)


def _manifest(tmp_path):
    tasks = [{"id": f"task-{index}", "category": category}
             for index, category in enumerate(_CATEGORIES)]
    target = tmp_path / "mix.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(
            {
                "categories": [
                    {"id": category, "delegation_expected": category != "do-not-delegate"}
                    for category in _CATEGORIES
                ],
                "tasks": tasks,
            }
        ),
        encoding="utf-8",
    )
    return target, {task["id"]: task["category"] for task in tasks}


def _session(store, session_id, *, status="success", cost=1.0, tokens=1000, attributed=True):
    directory = store.session_dir(session_id)
    directory.mkdir(parents=True)
    (directory / "meta.json").write_text(
        json.dumps({"status": status, "turns": 1, "metrics": {"tool_ms_total": 5}}),
        encoding="utf-8",
    )
    payload = {
        "content": "raw model transcript must not reach the report",
        "model": "provider/fixed-model",
        "usage": {"prompt_tokens": tokens, "completion_tokens": 10, "cost_usd": cost},
        "duration_ms": 20,
    }
    if attributed:
        payload.update({"model_binding_role": "reasoning", "call_purpose": "controller"})
    events = [
        {"type": "model_response", "timestamp": "2026-09-29T00:00:00+00:00", "payload": payload},
        {"type": "verification", "timestamp": "2026-09-29T00:00:01+00:00", "payload": {"approved": True}},
    ]
    (directory / "events.jsonl").write_text(
        "\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8"
    )


def _refs(store, *, bad_candidate=False):
    baselines, candidates = [], []
    for index, _category in enumerate(_CATEGORIES):
        task_id = f"task-{index}"
        base_id, candidate_id = f"base-{index}", f"candidate-{index}"
        _session(store, base_id)
        _session(
            store,
            candidate_id,
            cost=None if bad_candidate and index == 0 else 0.7,
            tokens=600,
            attributed=not (bad_candidate and index == 0),
        )
        baselines.append(parse_trial_reference(f"{task_id}={base_id}", trial="baseline"))
        candidates.append(parse_trial_reference(f"{task_id}={candidate_id}", trial="candidate"))
    return baselines, candidates


def _build(tmp_path, *, bad_candidate=False):
    store = SessionStore(tmp_path / "sessions")
    manifest_path, assignments = _manifest(tmp_path)
    baselines, candidates = _refs(store, bad_candidate=bad_candidate)
    return build_paired_report(
        store=store,
        baselines=baselines,
        candidates=candidates,
        task_manifest=load_task_manifest(manifest_path),
        model_versions={"reasoning": "provider/model@2026-09-29"},
        price_source="provider invoice",
        prompt_revision="sha256:revision",
        evidence_scores={task_id: {"baseline": 0.9, "candidate": 0.9} for task_id in assignments},
    )


def test_builds_complete_report_without_raw_event_content(tmp_path):
    report = _build(tmp_path)

    assert report.release_gates_passed
    assert len(report.trials) == 10
    assert report.payload["comparison"]["tasks"] == 5
    assert report.payload["release_gates_passed"] is True
    assert report.payload["trial_sources"][0] == {
        "task_id": "task-0",
        "trial": "baseline",
        "session_id": "base-0",
    }
    assert "raw model transcript" not in json.dumps(report.payload)


def test_unknown_or_unattributed_call_is_visible_and_fails_gates(tmp_path):
    report = _build(tmp_path, bad_candidate=True)

    assert report.comparison.unpriced_tasks == 1
    assert report.comparison.attribution_gaps == 1
    assert not report.release_gates_passed


def test_report_refuses_incomplete_pair_and_existing_output(tmp_path):
    store = SessionStore(tmp_path / "sessions")
    manifest_path, assignments = _manifest(tmp_path)
    baselines, candidates = _refs(store)
    with pytest.raises(PairedReportError, match="does not have one baseline"):
        build_paired_report(
            store=store,
            baselines=baselines,
            candidates=candidates[:-1],
            task_manifest=assignments,
            model_versions={"reasoning": "provider/model@2026-09-29"},
            price_source="provider invoice",
            prompt_revision="sha256:revision",
        )

    report = _build(tmp_path / "complete")
    output = tmp_path / "report.json"
    write_paired_report(output, report)
    with pytest.raises(PairedReportError, match="refusing to overwrite"):
        write_paired_report(output, report)

    with pytest.raises(PairedReportError, match="missing categories"):
        build_paired_report(
            store=store,
            baselines=baselines,
            candidates=candidates,
            task_manifest={"only-task": "read-heavy"},
            model_versions={"reasoning": "provider/model@2026-09-29"},
            price_source="provider invoice",
            prompt_revision="sha256:revision",
        )

    with pytest.raises(PairedReportError, match="Invalid session id"):
        build_paired_report(
            store=store,
            baselines=[TrialReference("task-0", "baseline", "../../secrets")],
            candidates=candidates,
            task_manifest=assignments,
            model_versions={"reasoning": "provider/model@2026-09-29"},
            price_source="provider invoice",
            prompt_revision="sha256:revision",
        )


def test_manifest_and_session_inputs_fail_closed(tmp_path):
    manifest = tmp_path / "bad.json"
    manifest.write_text(json.dumps({"categories": []}), encoding="utf-8")
    with pytest.raises(PairedReportError, match="no categories"):
        load_task_manifest(manifest)
    with pytest.raises(PairedReportError, match="Invalid session id"):
        parse_trial_reference("task=../../secrets", trial="baseline")

    store = SessionStore(tmp_path / "sessions")
    directory = store.session_dir("running")
    directory.mkdir(parents=True)
    (directory / "meta.json").write_text(json.dumps({"status": "running", "turns": 0}))
    (directory / "events.jsonl").write_text("{}\n")
    manifest_path, assignments = _manifest(tmp_path / "running-manifest")
    refs = [parse_trial_reference(f"{task}=running", trial="baseline") for task in assignments]
    with pytest.raises(PairedReportError, match="not terminal"):
        build_paired_report(
            store=store,
            baselines=refs,
            candidates=[
                parse_trial_reference(f"{task}=running", trial="candidate") for task in assignments
            ],
            task_manifest=load_task_manifest(manifest_path),
            model_versions={"reasoning": "provider/model@2026-09-29"},
            price_source="provider invoice",
            prompt_revision="sha256:revision",
        )
