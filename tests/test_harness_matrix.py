"""Harness matrix tests for issue #44 (P1.12).

Fixture report generation with missing data: unknown costs never read as
zero, model and harness stay separate dimensions, prompts hash instead of
persisting, and single-trial cells show their n.
"""

import pytest

from garuda.eval.harness_matrix import (
    prompt_hash,
    record_trial,
    render_table,
    summarize,
)


def _trials():
    return [
        record_trial("t1", "write hello", model="openrouter/a/m", harness="native",
                     completed=True, cost_usd=0.004, latency_ms=900, approvals=1),
        record_trial("t1", "write hello", model=None, harness="codex",
                     completed=True, cost_usd=None, latency_ms=2100, approvals=2,
                     handoff="acknowledged"),
        record_trial("t2", "fix bug", model="openrouter/a/m", harness="native",
                     completed=False, cost_usd=0.002, latency_ms=None, approvals=0,
                     error="timeout"),
        record_trial("t2", "fix bug", model=None, harness="codex",
                     completed=None, cost_usd=None, latency_ms=None, approvals=0),
    ]


def test_unknown_cost_never_reads_as_zero():
    cells = {c.harness: c.to_dict() for c in summarize(_trials())}
    codex = cells["codex"]
    assert codex["cost_usd_known_sum"] == 0.0
    assert codex["cost_unknown"] == 2
    table = render_table(summarize(_trials()))
    assert "$0.0000 + 2 unknown" in table
    assert "unknown" in table


def test_model_and_harness_are_separate_dimensions():
    cells = summarize(_trials())
    assert {(c.model, c.harness) for c in cells} == {
        ("openrouter/a/m", "native"),
        (None, "codex"),
    }
    native = next(c for c in cells if c.harness == "native")
    assert native.model == "openrouter/a/m"
    codex = next(c for c in cells if c.harness == "codex")
    assert codex.model is None


def test_reproducible_without_content_and_labeled_n():
    trial = record_trial("t9", "secret prompt text", model="m", harness="native")
    assert trial.prompt_hash == prompt_hash("secret prompt text")
    assert "secret prompt text" not in str(trial.to_dict())
    table = render_table(summarize(_trials()))
    assert "| 2 |" in table


def test_handoff_and_latency_missing_data():
    cells = {c.harness: c.to_dict() for c in summarize(_trials())}
    assert cells["codex"]["handoffs_acknowledged"] == 1
    assert cells["codex"]["handoffs_attempted"] == 1
    assert cells["native"]["latency_ms_median"] == 900
    assert cells["native"]["latency_unknown"] == 1
    # The None completion is unknown, not failure: 1/1 known, 1 unknown.
    assert cells["codex"]["completion_rate"] == 1.0
    assert cells["codex"]["unknown_completions"] == 1
    table = render_table(summarize(_trials()))
    assert "+1 unknown" in table


def test_even_sized_median_is_statistical():
    from garuda.eval.harness_matrix import MatrixCell

    cell = MatrixCell(model="m", harness="h", trials=2, completed=2,
                      latency_ms=[100, 300])
    assert cell.to_dict()["latency_ms_median"] == 200.0
    odd = MatrixCell(model="m", harness="h", trials=3, completed=3,
                     latency_ms=[100, 300, 200])
    assert odd.to_dict()["latency_ms_median"] == 200
    assert MatrixCell(model="m", harness="h").to_dict()["latency_ms_median"] is None


def test_ingestion_validates_shapes():
    from garuda.eval.harness_matrix import HarnessTrial

    good = record_trial("t1", "hi", model="m", harness="h", completed=True)
    assert HarnessTrial.from_dict(good.to_dict()) == good
    bad_shapes = [
        {"harness": "h"},  # missing task_id
        {**good.to_dict(), "completed": "yes"},
        {**good.to_dict(), "cost_usd": "cheap"},
        {**good.to_dict(), "latency_ms": "fast"},
        {**good.to_dict(), "latency_ms": -5},
        {**good.to_dict(), "approvals": -1},
        {**good.to_dict(), "capabilities": "prompt"},
        {**good.to_dict(), "prompt_hash": ""},
        {**good.to_dict(), "prompt_hash": "not-hex!!"},
        "not a mapping",
    ]
    for shape in bad_shapes:
        with pytest.raises(ValueError):
            HarnessTrial.from_dict(shape)


async def test_trials_come_from_real_eval_runs(tmp_path, monkeypatch):
    """`trials_from_ablation` populates the comparison from measured runs:
    ungraded successes stay unknown, hashes match, costs stay unknown."""
    import garuda.eval.ablation as ablation_mod
    from garuda.eval.ablation import AblationTask, VariantResult, run_ablation
    from garuda.eval.harness_matrix import trials_from_ablation

    async def _script_variant(task, variant, overrides, model_name, base_dir):
        return VariantResult(
            variant=variant, task_id=task.id, agent_success=True,
            graded_pass=None, turns=1, prompt_tokens=10, completion_tokens=5,
            total_tokens=15, duration_ms=120, error=None,
        )

    monkeypatch.setattr(ablation_mod, "run_variant", _script_variant)
    task = AblationTask(id="t1", prompt="write hello", check=None)
    results = await run_ablation([task], {"baseline": {}}, "script/test",
                                 base_dir=tmp_path)
    trials = trials_from_ablation([task], results, model="script/test")
    assert len(trials) == 1
    (trial,) = trials
    assert trial.completed is None
    assert trial.prompt_hash == prompt_hash("write hello")
    assert trial.cost_usd is None
    cells = summarize(trials)
    assert cells[0].to_dict()["unknown_completions"] == 1
    assert cells[0].to_dict()["completion_rate"] is None
    assert cells[0].to_dict()["latency_ms_median"] == 120
