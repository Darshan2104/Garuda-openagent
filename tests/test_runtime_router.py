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
    with pytest.raises(RoutingError, match="over budget"):
        route(_pool(), RoutingRequest(pin="native", budget_usd=0.001))


def test_duplicate_invalid_and_workspace_blocked_candidates_refuse():
    with pytest.raises(RoutingError, match="duplicate runtime"):
        route([RoutingCandidate("x"), RoutingCandidate("x")], RoutingRequest())
    with pytest.raises(RoutingError, match="quality history"):
        RoutingCandidate("x", successes=2, trials=1)
    with pytest.raises(RoutingError, match="cost_usd"):
        RoutingCandidate("x", cost_usd=-1)
    blocked = [
        RoutingCandidate(
            "locked",
            capabilities=("prompt",),
            cost_usd=0.0,
            mutating_allowed=False,
        ),
        RoutingCandidate("reader", capabilities=("prompt",), cost_usd=0.1),
    ]
    decision = route(blocked, RoutingRequest(required_capabilities=("prompt",)))
    assert decision.selected == "reader"
    assert any("mutation not allowed" in line for line in decision.rationale)
    assert route(
        blocked,
        RoutingRequest(required_capabilities=("prompt",), mutating=False),
    ).selected == "locked"


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


def test_non_finite_cost_and_budget_refuse():
    with pytest.raises(RoutingError, match="finite"):
        RoutingCandidate("x", cost_usd=float("nan"))
    with pytest.raises(RoutingError, match="finite"):
        RoutingCandidate("x", cost_usd=float("inf"))
    with pytest.raises(RoutingError, match="finite"):
        RoutingRequest(budget_usd=float("nan"))


def test_resolve_and_record_persists_on_session(tmp_path, monkeypatch):
    from garuda.core.sessions import SessionStore
    from garuda.runtime.router import resolve_and_record_routing

    settings = tmp_path / "settings.yaml"
    settings.write_text("routing:\n  enabled: true\n", encoding="utf-8")
    monkeypatch.setenv("GARUDA_GLOBAL_SETTINGS", str(settings))
    monkeypatch.setenv("GARUDA_SESSIONS_DIR", str(tmp_path / "sessions"))
    store = SessionStore()
    store.begin("s1", task="t", model="m", agent="a", workspace=str(tmp_path))
    decision = resolve_and_record_routing(
        workspace=str(tmp_path),
        store=store,
        session_id="s1",
        pin="native",
        enabled=True,
    )
    assert decision is not None
    assert decision.selected == "native"
    meta = store.load_meta("s1")
    assert meta["routing_decision"]["selected"] == "native"
    assert meta["routing_decision"]["rationale"]

    assert (
        resolve_and_record_routing(
            workspace=str(tmp_path),
            store=store,
            session_id="s1",
            enabled=False,
        )
        is None
    )


def test_pin_over_budget_refuses_on_product_path(tmp_path, monkeypatch):
    from garuda.core.sessions import SessionStore
    from garuda.runtime.router import resolve_and_record_routing

    monkeypatch.setenv("GARUDA_GLOBAL_SETTINGS", str(tmp_path / "missing.yaml"))
    monkeypatch.setenv("GARUDA_SESSIONS_DIR", str(tmp_path / "sessions"))
    store = SessionStore()
    store.begin("s1", task="t", model="m", agent="a", workspace=str(tmp_path))
    with pytest.raises(RoutingError, match="over budget"):
        resolve_and_record_routing(
            workspace=str(tmp_path),
            store=store,
            session_id="s1",
            pin="native",
            enabled=True,
            budget_usd=0.0,
            costs={"native": 0.01},
        )
