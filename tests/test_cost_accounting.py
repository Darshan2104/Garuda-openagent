"""Cost accounting: cache-aware pricing, provider passthrough, and overrides.

The regression these pin down: an agentic run is ~97% cache reads, and charging
every prompt token at the fresh-input rate overstated spend roughly fivefold.
"""

import json

from garuda.eval.atif_export import events_to_atif
from garuda.eval.costs import estimate_cost, merge_usage, provider_cost

MODEL = "openrouter/minimax/minimax-m2.5"

# A real trial from the 50-task benchmark.
LZ77 = {"prompt_tokens": 134964, "completion_tokens": 9002, "cache_read_tokens": 125376}

MEASURED = json.dumps(
    {"minimax-m2.5": {"input": 0.3024, "cache_read": 0.0331, "output": 1.3305}}
)


# --- tier 1: what the provider charged -------------------------------------


def test_provider_reported_cost_wins():
    """The invoice beats any model of the invoice."""
    usage = {**LZ77, "cost_usd": 0.0173}
    assert provider_cost(usage) == 0.0173
    assert estimate_cost(MODEL, usage) == 0.0173


def test_provider_cost_used_even_when_the_model_is_unknown():
    assert estimate_cost("some/unlisted-model", {"cost_usd": 0.5}) == 0.5


def test_nonsense_provider_cost_is_ignored():
    for bad in (None, "free", -1, [0.2]):
        assert provider_cost({"cost_usd": bad}) is None


# --- tier 2: local overrides -----------------------------------------------


def test_override_rates_are_applied(monkeypatch):
    monkeypatch.setenv("GARUDA_TOKEN_PRICES", MEASURED)
    expected = (
        (134964 - 125376) * 0.3024e-6 + 125376 * 0.0331e-6 + 9002 * 1.3305e-6
    )
    assert estimate_cost(MODEL, LZ77) == round(expected, 8)


def test_override_matches_on_substring_longest_first(monkeypatch):
    monkeypatch.setenv(
        "GARUDA_TOKEN_PRICES",
        json.dumps(
            {
                "minimax": {"input": 100.0, "cache_read": 100.0, "output": 100.0},
                "minimax-m2.5": {"input": 1.0, "cache_read": 1.0, "output": 1.0},
            }
        ),
    )
    cost = estimate_cost(MODEL, {"prompt_tokens": 1_000_000, "completion_tokens": 0})
    assert cost == 1.0  # the more specific key won


def test_malformed_override_falls_back_instead_of_crashing(monkeypatch):
    monkeypatch.setenv("GARUDA_TOKEN_PRICES", "{not json")
    assert estimate_cost(MODEL, LZ77) is not None


def test_partial_override_defaults_cache_rate_to_input_rate(monkeypatch):
    monkeypatch.setenv("GARUDA_TOKEN_PRICES", json.dumps({"minimax-m2.5": {"input": 1.0}}))
    # 1M prompt of which 1M cached, no explicit cache rate -> priced as input.
    cost = estimate_cost(
        MODEL, {"prompt_tokens": 1_000_000, "completion_tokens": 0, "cache_read_tokens": 1_000_000}
    )
    assert cost == 1.0


# --- tier 3: the pricing table, cache-aware --------------------------------


def test_cached_tokens_are_not_charged_at_the_fresh_rate(monkeypatch):
    monkeypatch.delenv("GARUDA_TOKEN_PRICES", raising=False)
    cache_aware = estimate_cost(MODEL, LZ77)
    naive = estimate_cost(MODEL, {k: v for k, v in LZ77.items() if k != "cache_read_tokens"})
    assert cache_aware < naive
    # The old behaviour reported $0.0504 for this trial.
    assert cache_aware < 0.04


def test_cost_scales_with_the_uncached_share(monkeypatch):
    monkeypatch.delenv("GARUDA_TOKEN_PRICES", raising=False)
    mostly_cached = estimate_cost(
        MODEL, {"prompt_tokens": 100_000, "completion_tokens": 0, "cache_read_tokens": 99_000}
    )
    uncached = estimate_cost(MODEL, {"prompt_tokens": 100_000, "completion_tokens": 0})
    assert mostly_cached < uncached


# --- guards ----------------------------------------------------------------


def test_no_number_is_invented_without_inputs(monkeypatch):
    monkeypatch.delenv("GARUDA_TOKEN_PRICES", raising=False)
    assert estimate_cost(MODEL, None) is None
    assert estimate_cost(MODEL, {}) is None
    assert estimate_cost(None, LZ77) is None
    assert estimate_cost(MODEL, {"prompt_tokens": 0, "completion_tokens": 0}) is None


def test_cached_exceeding_prompt_does_not_go_negative(monkeypatch):
    monkeypatch.delenv("GARUDA_TOKEN_PRICES", raising=False)
    cost = estimate_cost(
        MODEL, {"prompt_tokens": 100, "completion_tokens": 0, "cache_read_tokens": 500}
    )
    assert cost is not None and cost >= 0


def test_merge_usage_ignores_the_cost_field():
    totals: dict = {}
    merge_usage(totals, {"prompt_tokens": 10, "cache_read_tokens": 4, "cost_usd": 0.5})
    assert totals == {"prompt_tokens": 10, "cache_read_tokens": 4}


# --- export ----------------------------------------------------------------


def _events(usages):
    events = [
        {
            "type": "session_start",
            "timestamp": "2026-07-27T10:00:00+00:00",
            "payload": {"task": "t", "model": MODEL},
        }
    ]
    for i, usage in enumerate(usages):
        events.append(
            {
                "type": "model_response",
                "timestamp": f"2026-07-27T10:00:0{i + 1}+00:00",
                "payload": {"content": "step", "tool_calls": [], "usage": usage},
            }
        )
    return events


def test_trajectory_total_sums_provider_costs(monkeypatch):
    monkeypatch.delenv("GARUDA_TOKEN_PRICES", raising=False)
    usages = [
        {"prompt_tokens": 1000, "completion_tokens": 10, "cache_read_tokens": 900, "cost_usd": 0.01},
        {"prompt_tokens": 2000, "completion_tokens": 20, "cache_read_tokens": 1900, "cost_usd": 0.02},
    ]
    atif = events_to_atif(_events(usages), session_id="s", model_name=MODEL)
    assert atif["final_metrics"]["total_cost_usd"] == 0.03
    assert atif["final_metrics"]["total_cached_tokens"] == 2800


def test_trajectory_total_is_cache_aware_without_provider_costs(monkeypatch):
    monkeypatch.setenv("GARUDA_TOKEN_PRICES", MEASURED)
    usages = [{"prompt_tokens": 1_000_000, "completion_tokens": 0, "cache_read_tokens": 900_000}]
    atif = events_to_atif(_events(usages), session_id="s", model_name=MODEL)
    expected = 100_000 * 0.3024e-6 + 900_000 * 0.0331e-6
    assert atif["final_metrics"]["total_cost_usd"] == round(expected, 8)


def test_explicit_cost_argument_still_wins(monkeypatch):
    monkeypatch.delenv("GARUDA_TOKEN_PRICES", raising=False)
    usages = [{"prompt_tokens": 1000, "completion_tokens": 10, "cost_usd": 0.01}]
    atif = events_to_atif(_events(usages), session_id="s", model_name=MODEL, cost_usd=0.99)
    assert atif["final_metrics"]["total_cost_usd"] == 0.99
