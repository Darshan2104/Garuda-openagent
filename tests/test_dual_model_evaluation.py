"""Paired dual-model evaluation: unknown cost is never zero, gates are exact."""

from garuda.eval.dual_model import (
    PairedComparison,
    PairedResult,
    compare_trials,
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
