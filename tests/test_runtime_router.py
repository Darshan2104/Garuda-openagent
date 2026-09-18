"""Policy router tests for issue #49 (P2.3).

Deterministic ranking, pinning, unknown-cost semantics, and confirmed
boundary-only switching.
"""

import pytest

from garuda.runtime.router import (
    RoutingCandidate,
    RoutingDecision,
    RoutingError,
    RoutingRequest,
    authorize_switch,
    route,
)


def _pool() -> list[RoutingCandidate]:
    return [
        RoutingCandidate("native", available=True, capabilities=("prompt", "cancel"),
                         cost_usd=0.004, successes=8, trials=10),
        RoutingCandidate("codex", available=True, capabilities=("prompt", "cancel"),
                         cost_usd=None, successes=4, trials=5),
        RoutingCandidate("pi", available=True, capabilities=("prompt",),
                         cost_usd=0.001, successes=0, trials=0),
        RoutingCandidate("down", available=False, capabilities=("prompt", "cancel"),
                         unavailable_reason="binary missing"),
    ]


def test_deterministic_ranking_with_rationale():
    first = route(_pool(), RoutingRequest(required_capabilities=("prompt", "cancel")))
    second = route(_pool(), RoutingRequest(required_capabilities=("prompt", "cancel")))
    assert first == second
    assert first.selected == "native"
    assert first.ranking[0] == "native"
    assert "down" not in first.ranking
    assert any("availability" in line for line in first.rationale)
    assert first.to_dict()["auto_switch"] is False


def test_unknown_cost_never_reads_free():
    decision = route(
        _pool(),
        RoutingRequest(required_capabilities=("prompt",), budget_usd=0.002),
    )
    assert decision.selected == "pi"
    assert "codex" in decision.ranking
    assert decision.ranking.index("pi") < decision.ranking.index("codex")
    assert any("unknown (not free)" in line for line in decision.rationale)


def test_budget_refusal_and_capability_refusal():
    pool = [c for c in _pool() if c.cost_usd is not None]
    with pytest.raises(RoutingError, match="within budget"):
        route(pool, RoutingRequest(required_capabilities=("prompt",), budget_usd=0.0))
    with pytest.raises(RoutingError, match="no available runtime"):
        route(_pool(), RoutingRequest(required_capabilities=("teleport",)))


def test_pin_wins_or_fails_loudly():
    decision = route(_pool(), RoutingRequest(pin="pi"))
    assert decision.selected == "pi"
    assert decision.ranking == ("pi",)
    with pytest.raises(RoutingError, match="unavailable"):
        route(_pool(), RoutingRequest(pin="down"))
    with pytest.raises(RoutingError, match="not configured"):
        route(_pool(), RoutingRequest(pin="ghost"))
    with pytest.raises(RoutingError, match="lacks capabilities"):
        route(_pool(), RoutingRequest(pin="pi", required_capabilities=("cancel",)))


def test_switch_needs_confirmation_and_boundary():
    decision = route(_pool(), RoutingRequest(pin="pi"))
    with pytest.raises(RoutingError, match="explicit confirmation"):
        authorize_switch(decision, confirmed=False, source_state="idle")
    with pytest.raises(RoutingError, match="boundary-only"):
        authorize_switch(decision, confirmed=True, source_state="running")
    authorized = authorize_switch(decision, confirmed=True, source_state="paused_at_boundary")
    assert isinstance(authorized, RoutingDecision)
    assert authorized.auto_switch is True
    assert authorized.selected == "pi"
