"""Harness matrix tests for issue #44 (P1.12).

Fixture report generation with missing data: unknown costs never read as
zero, model and harness stay separate dimensions, prompts hash instead of
persisting, and single-trial cells show their n.
"""

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
    assert cells["codex"]["completion_rate"] == 0.5
