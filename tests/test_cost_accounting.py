"""Cost accounting: cache-aware pricing, provider passthrough, and overrides.

The regression these pin down: an agentic run is ~97% cache reads, and charging
every prompt token at the fresh-input rate overstated spend roughly fivefold.

Nothing here asserts against litellm's pricing table. It is fetched over the
network at import time and edited continuously upstream, so a test written
against it asserts on someone else's repository — it passed on the machine it was
written on and failed on the reviewer's. The rates come from the in-repo snapshot
instead, and the arithmetic is exercised against a fixture table.
"""

import json

import pytest

from garuda.eval import pricing
from garuda.eval.atif_export import events_to_atif
from garuda.eval.costs import estimate_cost, merge_usage, provider_cost

MODEL = "openrouter/minimax/minimax-m2.5"

# A fixture table, so the arithmetic is checked against numbers this file owns.
# Round figures: a wrong rate should be visible in the assertion, not derived.
FIXTURE_MODEL = "fixture/pricing-probe"
FIXTURE_RATES = {
    "input": 1e-06,
    "cache_read": 1e-07,  # a tenth of input, as cache discounts run
    "cache_write": 2e-06,
    "output": 1e-05,
}


@pytest.fixture
def fixture_prices(monkeypatch):
    """Replace the snapshot with a table of known rates."""
    monkeypatch.delenv("GARUDA_TOKEN_PRICES", raising=False)
    monkeypatch.setattr(pricing, "SNAPSHOT", {"pricing-probe": FIXTURE_RATES})
    return FIXTURE_RATES

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


# --- tier 3: the versioned snapshot, cache-aware ---------------------------


def test_snapshot_prices_every_token_class(fixture_prices):
    cost = estimate_cost(
        FIXTURE_MODEL,
        {
            "prompt_tokens": 1_000_000,  # inclusive of the cached reads
            "cache_read_tokens": 900_000,
            "cache_creation_tokens": 50_000,
            "completion_tokens": 10_000,
        },
    )
    expected = 100_000 * 1e-06 + 900_000 * 1e-07 + 50_000 * 2e-06 + 10_000 * 1e-05
    assert cost == round(expected, 8)


def test_cached_tokens_are_not_charged_at_the_fresh_rate(fixture_prices):
    cache_aware = estimate_cost(FIXTURE_MODEL, LZ77)
    naive = estimate_cost(FIXTURE_MODEL, {k: v for k, v in LZ77.items() if k != "cache_read_tokens"})
    assert cache_aware < naive
    # 125,376 of 134,964 prompt tokens are cache reads, at a tenth of the rate.
    assert cache_aware == round(9588 * 1e-06 + 125_376 * 1e-07 + 9002 * 1e-05, 8)


def test_cost_scales_with_the_uncached_share(fixture_prices):
    mostly_cached = estimate_cost(
        FIXTURE_MODEL,
        {"prompt_tokens": 100_000, "completion_tokens": 0, "cache_read_tokens": 99_000},
    )
    uncached = estimate_cost(FIXTURE_MODEL, {"prompt_tokens": 100_000, "completion_tokens": 0})
    assert mostly_cached < uncached


def test_snapshot_rate_missing_for_a_class_falls_back_to_the_input_rate(monkeypatch):
    """A provider that does not bill cache reads separately must not bill them at zero."""
    monkeypatch.delenv("GARUDA_TOKEN_PRICES", raising=False)
    monkeypatch.setattr(pricing, "SNAPSHOT", {"pricing-probe": {"input": 1e-06, "output": 0.0}})
    cost = estimate_cost(
        FIXTURE_MODEL,
        {"prompt_tokens": 1_000_000, "completion_tokens": 0, "cache_read_tokens": 1_000_000},
    )
    assert cost == 1.0


def test_snapshot_matches_on_substring_longest_first(monkeypatch):
    monkeypatch.delenv("GARUDA_TOKEN_PRICES", raising=False)
    monkeypatch.setattr(
        pricing,
        "SNAPSHOT",
        {
            "probe": {"input": 100e-06, "output": 0.0},
            "pricing-probe": {"input": 1e-06, "output": 0.0},
        },
    )
    cost = estimate_cost(FIXTURE_MODEL, {"prompt_tokens": 1_000_000, "completion_tokens": 0})
    assert cost == 1.0  # the more specific key won


def test_env_override_still_beats_the_snapshot(monkeypatch):
    monkeypatch.setattr(pricing, "SNAPSHOT", {"pricing-probe": FIXTURE_RATES})
    monkeypatch.setenv(
        "GARUDA_TOKEN_PRICES", json.dumps({"pricing-probe": {"input": 2.0, "output": 0.0}})
    )
    cost = estimate_cost(FIXTURE_MODEL, {"prompt_tokens": 1_000_000, "completion_tokens": 0})
    assert cost == 2.0


# --- the reproducibility guarantee itself ----------------------------------


def test_the_benchmark_model_is_priced_without_consulting_litellm(monkeypatch):
    """Benchmark economics must not depend on a table fetched from the network.

    The regression: pricing fell through to litellm, whose cost map is fetched
    from GitHub at import and revised upstream continuously. The same trajectory
    then priced differently on two machines, and cache-discount assertions passed
    or failed according to which map each one happened to receive.
    """
    monkeypatch.delenv("GARUDA_TOKEN_PRICES", raising=False)

    def _explode():
        raise AssertionError("snapshot-priced models must never reach litellm")

    monkeypatch.setattr("garuda.eval.costs._import_litellm", _explode)

    cost = estimate_cost(MODEL, LZ77)
    rates = pricing.snapshot_rates(MODEL)
    assert rates is not None
    expected = (
        9588 * rates["input"] + 125_376 * rates["cache_read"] + 9002 * rates["output"]
    )
    assert cost == round(expected, 8)
    # The old behaviour reported $0.0504 for this trial.
    assert cost < 0.04


def test_litellm_is_pinned_to_its_bundled_table(monkeypatch):
    """The fallback for unlisted models is a function of the installed version."""
    import os

    from garuda.eval import costs

    # setenv first so monkeypatch records the variable and restores it on teardown.
    monkeypatch.setenv(costs._LITELLM_LOCAL_MAP_ENV, "recorded")
    monkeypatch.delenv(costs._LITELLM_LOCAL_MAP_ENV)
    costs._import_litellm()
    assert os.environ[costs._LITELLM_LOCAL_MAP_ENV] == "True"


def test_an_explicit_litellm_map_choice_is_left_alone(monkeypatch):
    """A host that has deliberately opted into the network map keeps it."""
    import os

    from garuda.eval import costs

    monkeypatch.setenv(costs._LITELLM_LOCAL_MAP_ENV, "False")
    costs._import_litellm()
    assert os.environ[costs._LITELLM_LOCAL_MAP_ENV] == "False"


def test_snapshot_rates_are_well_formed():
    """A typo'd key or a rate in the wrong unit is a silent five-figure error."""
    assert pricing.SNAPSHOT_VERSION
    for model, rates in pricing.SNAPSHOT.items():
        assert rates.get("input") is not None, f"{model} has no input rate"
        assert rates.get("output") is not None, f"{model} has no output rate"
        for field_name, rate in rates.items():
            assert field_name in ("input", "cache_read", "cache_write", "output"), field_name
            # Per *token*: anything above a cent per token is a per-million figure
            # that was never divided down.
            assert 0 <= rate < 0.01, f"{model}.{field_name}={rate} looks like a per-million rate"


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
