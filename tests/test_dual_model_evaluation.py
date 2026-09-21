"""Paired dual-model evaluation: unknown cost is never zero, gates are exact."""

import json

import pytest

from garuda.eval.dual_model import (
    PAIRED_REPORT_FILENAME,
    REPRESENTATIVE_TASK_CATEGORIES,
    TASK_MIX_VERSION,
    PairedComparison,
    PairedResult,
    aggregate_role_costs,
    compare_trials,
    fixtures_dir,
    format_comparison,
    load_task_mix,
    role_cost_total,
)


def _result(task, trial, **kw):
    base = dict(
        task_id=task,
        trial=trial,
        success=True,
        total_tokens=1000,
        total_cost_usd=1.0,
        reasoning_tokens=800,
        reasoning_cost_usd=0.9,
    )
    base.update(kw)
    return PairedResult(**base)


def test_unknown_cost_is_excluded_not_zero():
    base = _result("t1", "baseline")
    cand = _result("t1", "candidate", total_cost_usd=None, cost_unknown_reason="subscription usage")
    comp = compare_trials([base], [cand])
    assert comp.priced_tasks == 0
    assert comp.unpriced_tasks == 1
    assert comp.median_total_cost_saving is None
    assert not comp.passes_release_gates()


def test_zero_priced_tasks_fail_gates():
    comp = PairedComparison(tasks=2, baseline_success=2, candidate_success=2)
    assert not comp.passes_release_gates()


def test_release_gates_require_all_thresholds():
    comp = PairedComparison(
        tasks=10,
        baseline_success=8,
        candidate_success=8,
        median_total_cost_saving=0.25,
        median_reasoning_input_saving=0.35,
        priced_tasks=10,
    )
    assert comp.passes_release_gates()
    comp.median_total_cost_saving = 0.10
    assert not comp.passes_release_gates()


def test_completion_regression_gate():
    comp = PairedComparison(
        tasks=100,
        baseline_success=80,
        candidate_success=77,  # 3pt regression > 2pt allowance
        median_total_cost_saving=0.5,
        median_reasoning_input_saving=0.5,
        priced_tasks=100,
    )
    assert not comp.passes_release_gates()


def test_compare_trials_pairs_by_task_and_medians():
    baselines = [
        _result("a", "baseline", total_cost_usd=1.0, reasoning_tokens=1000),
        _result("b", "baseline", total_cost_usd=2.0, reasoning_tokens=1000),
    ]
    candidates = [
        _result("a", "candidate", total_cost_usd=0.7, reasoning_tokens=600),  # 30% / 40%
        _result("b", "candidate", total_cost_usd=1.6, reasoning_tokens=600),  # 20% / 40%
    ]
    comp = compare_trials(baselines, candidates)
    assert comp.tasks == 2
    assert comp.priced_tasks == 2
    assert comp.median_total_cost_saving == 0.25
    assert comp.median_reasoning_input_saving == 0.4


def _passing_comparison(**kw):
    base = dict(
        tasks=10,
        baseline_success=8,
        candidate_success=8,
        median_total_cost_saving=0.25,
        median_reasoning_input_saving=0.35,
        priced_tasks=10,
    )
    base.update(kw)
    return PairedComparison(**base)


def test_reasoning_input_tokens_are_the_release_metric():
    baselines = [_result("a", "baseline", reasoning_tokens=1000, reasoning_input_tokens=900)]
    candidates = [_result("a", "candidate", reasoning_tokens=100, reasoning_input_tokens=600)]
    comp = compare_trials(baselines, candidates)
    # (900 - 600) / 900 on the input split, not the legacy token total.
    assert comp.median_reasoning_input_saving == pytest.approx(1 / 3)


def test_reasoning_input_falls_back_to_reasoning_tokens():
    assert _result("a", "baseline").reasoning_input() == 800
    legacy = _result("a", "baseline", reasoning_tokens=500, reasoning_input_tokens=None)
    assert legacy.reasoning_input() == 500


def test_per_role_costs_propagate_unknown_never_zero():
    assert aggregate_role_costs([0.5, 0.2, 0.1]) == pytest.approx(0.8)
    assert aggregate_role_costs([0.5, None, 0.1]) is None
    priced = _result("a", "baseline", reasoning_cost_usd=0.5,
                     collection_cost_usd=0.2, classifier_cost_usd=0.1)
    assert role_cost_total(priced) == pytest.approx(0.8)
    external = _result("a", "candidate", reasoning_cost_usd=None,
                       collection_cost_usd=None, classifier_cost_usd=None,
                       total_cost_usd=None, cost_unknown_reason="subscription usage")
    assert role_cost_total(external) is None


def test_attribution_gap_fails_release_gates():
    assert _passing_comparison().passes_release_gates()
    assert not _passing_comparison(attribution_gaps=1).passes_release_gates()


def test_unattested_trials_are_counted_not_silent():
    baselines = [_result("a", "baseline")]
    candidates = [_result("a", "candidate", total_cost_usd=0.7,
                          reasoning_tokens=600, attribution_complete=None)]
    comp = compare_trials(baselines, candidates)
    assert comp.attribution_unknown == 1
    assert comp.attribution_gaps == 0
    assert any("attestation" in note for note in comp.notes)


def test_collection_mutations_fail_release_gates():
    assert _passing_comparison().passes_release_gates()
    assert not _passing_comparison(collection_mutations=1).passes_release_gates()
    comp = compare_trials(
        [_result("a", "baseline")],
        [_result("a", "candidate", total_cost_usd=0.7, reasoning_tokens=600,
                 collection_mutations=2)],
    )
    assert comp.collection_mutations == 2
    assert not comp.passes_release_gates()


def test_investigation_decline_fails_release_gates():
    assert _passing_comparison(
        baseline_investigation=100, candidate_investigation=95).passes_release_gates()
    assert not _passing_comparison(
        baseline_investigation=100, candidate_investigation=80).passes_release_gates()


def test_evidence_decline_fails_release_gates():
    assert _passing_comparison(
        baseline_evidence_avg=0.8, candidate_evidence_avg=0.78).passes_release_gates()
    assert not _passing_comparison(
        baseline_evidence_avg=0.8, candidate_evidence_avg=0.70).passes_release_gates()


def test_evidence_score_rejects_out_of_range():
    with pytest.raises(ValueError):
        _result("a", "baseline", evidence_score=1.5)


def test_fallback_and_stale_rates_are_visible():
    baselines = [_result("a", "baseline"), _result("b", "baseline")]
    candidates = [
        _result("a", "candidate", fallbacks=1),
        _result("b", "candidate", stale_reports=1),
    ]
    comp = compare_trials(baselines, candidates)
    assert comp.fallback_rate() == pytest.approx(0.5)
    assert comp.stale_rate() == pytest.approx(0.5)
    report = format_comparison(comp)
    assert "fallbacks=1 (rate=50.0%)" in report
    assert "stale=1 (rate=50.0%)" in report
    assert "attribution gaps=0" in report
    assert "collection_mutations=0" in report


def test_paired_result_round_trip():
    full = _result(
        "a", "candidate",
        verification_passed=True,
        collection_tokens=11000,
        collection_cost_usd=0.13,
        classifier_tokens=1000,
        classifier_cost_usd=0.01,
        wall_ms=140000,
        model_ms=95000,
        tool_ms=35000,
        file_reads=32,
        searches=12,
        collection_jobs=3,
        fallbacks=1,
        stale_reports=1,
        collection_mutations=0,
        reasoning_input_tokens=7200,
        evidence_score=0.86,
        attribution_complete=True,
    )
    assert PairedResult.from_dict(full.to_dict()) == full


def test_task_mix_fixture_covers_representative_classes():
    mix = load_task_mix()
    assert mix["mix_version"] == TASK_MIX_VERSION
    by_id = {entry["id"]: entry for entry in mix["categories"]}
    assert REPRESENTATIVE_TASK_CATEGORIES <= set(by_id)
    assert by_id["do-not-delegate"]["delegation_expected"] is False
    assert all(entry["delegation_expected"] is True
               for key, entry in by_id.items() if key != "do-not-delegate")


def test_task_mix_loader_rejects_narrowed_mixes(tmp_path):
    narrowed = {"categories": [{"id": "read-heavy", "delegation_expected": True}]}
    target = tmp_path / "mix.json"
    target.write_text(json.dumps(narrowed), encoding="utf-8")
    with pytest.raises(ValueError, match="missing categories"):
        load_task_mix(target)


def test_offline_fixture_report_is_reproducible():
    payload = json.loads((fixtures_dir() / PAIRED_REPORT_FILENAME).read_text(encoding="utf-8"))

    def _build():
        baselines = [PairedResult.from_dict(t) for t in payload["trials"] if t["trial"] == "baseline"]
        candidates = [PairedResult.from_dict(t) for t in payload["trials"] if t["trial"] == "candidate"]
        return compare_trials(baselines, candidates)

    first, second = _build(), _build()
    assert first.to_dict() == second.to_dict()
    assert first.tasks == 6
    assert first.priced_tasks == 5
    assert first.unpriced_tasks == 1
    assert first.median_total_cost_saving == pytest.approx(0.25)
    assert first.median_reasoning_input_saving == pytest.approx(0.35)
    assert first.fallback_rate() == pytest.approx(1 / 6)
    assert first.stale_rate() == pytest.approx(1 / 6)
    assert first.collection_mutations == 0
    assert first.attribution_gaps == 0
    assert any("external subscription" in note for note in first.notes)
    assert first.passes_release_gates()
    assert "release gates=PASS" in format_comparison(first)
