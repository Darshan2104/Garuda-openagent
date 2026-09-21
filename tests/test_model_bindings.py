"""Model-role bindings: precedence, legacy compat, and trust."""

import pytest

from garuda.model.config import (
    CollectionBudget,
    CollectionPolicy,
    ConfigError,
    ModelBindings,
    ModelSpec,
    check_project_trust,
    narrow_collection_policy,
    parse_collection_policy,
    parse_model_spec,
    resolve_model_bindings,
    with_compat_reasoning_settings,
)


def test_empty_resolves_builtin_reasoning_and_no_collection():
    bindings, prov = resolve_model_bindings(env={})
    assert bindings.reasoning.model == "openrouter/deepseek/deepseek-v4-flash-0731"
    assert bindings.collection is None


def test_legacy_garuda_model_kept_for_reasoning():
    bindings, _ = resolve_model_bindings(env={"GARUDA_MODEL": "openrouter/a/m"})
    assert bindings.reasoning.model == "openrouter/a/m"


def test_reasoning_env_beats_legacy():
    bindings, _ = resolve_model_bindings(
        env={"GARUDA_MODEL": "openrouter/a/m", "GARUDA_REASONING_MODEL": "openrouter/b/m"}
    )
    assert bindings.reasoning.model == "openrouter/b/m"


def test_explicit_beats_env():
    bindings, prov = resolve_model_bindings(
        explicit_reasoning="openrouter/x/m",
        env={"GARUDA_REASONING_MODEL": "openrouter/b/m", "GARUDA_MODEL": "openrouter/a/m"},
    )
    assert bindings.reasoning.model == "openrouter/x/m"
    assert prov["reasoning"].provenance.value == "explicit"


def test_route_profile_project_global_order():
    def mk(m):
        return ModelBindings(reasoning=ModelSpec(model=m))

    bindings, prov = resolve_model_bindings(
        env={},
        route_binding=mk("openrouter/route/m"),
        profile_binding=mk("openrouter/profile/m"),
        project_binding=mk("openrouter/project/m"),
        global_binding=mk("openrouter/global/m"),
    )
    assert bindings.reasoning.model == "openrouter/route/m"
    bindings, _ = resolve_model_bindings(env={}, profile_binding=mk("openrouter/p/m"))
    assert bindings.reasoning.model == "openrouter/p/m"


def test_no_collection_flag_wins():
    bindings, _ = resolve_model_bindings(
        explicit_collection="openrouter/c/m", no_collection=True, env={}
    )
    assert bindings.collection is None


def test_project_cannot_authorize_new_provider():
    glob = {"strong": ModelSpec(model="provider-a/m")}
    with pytest.raises(ConfigError):
        check_project_trust(
            project_models={"evil": ModelSpec(model="provider-b/m")},
            global_models=glob, source="project settings",
        )


def test_project_cannot_set_api_base():
    glob = {"strong": ModelSpec(model="provider-a/m")}
    with pytest.raises(ConfigError):
        check_project_trust(
            project_models={"x": ModelSpec(model="provider-a/m", api_base="https://evil")},
            global_models=glob, source="project settings",
        )


def test_project_budget_cannot_widen():
    with pytest.raises(ConfigError):
        check_project_trust(
            project_models={}, global_models={},
            project_budget=CollectionBudget(max_jobs_per_run=10),
            global_budget=CollectionBudget(max_jobs_per_run=4),
            source="project settings",
        )


def test_unknown_transport_and_bad_limits_fail():
    with pytest.raises(ConfigError):
        parse_model_spec({"transport": "telepathy", "model": "x"}, source="t")
    with pytest.raises(ConfigError):
        parse_model_spec({"model": ""}, source="t")
    with pytest.raises(ConfigError):
        parse_collection_policy({"fallback": {"interactive": "always"}}, source="t")
    with pytest.raises(ConfigError):
        parse_collection_policy({"handoff": "full"}, source="t")


def test_api_key_in_spec_fails_actionably():
    with pytest.raises(ConfigError, match="must not store an API key"):
        parse_model_spec({"model": "a/m", "api_key": "sk-secret"}, source="global models.x")


def test_compat_reasoning_knobs_fill_only_gaps():
    spec = with_compat_reasoning_settings(
        ModelSpec(model="a/m"), reasoning_effort="high", thinking_budget_tokens=5000
    )
    assert spec.reasoning_effort == "high"
    assert spec.thinking_budget_tokens == 5000
    explicit = with_compat_reasoning_settings(
        ModelSpec(model="a/m", reasoning_effort="low"),
        reasoning_effort="high",
        thinking_budget_tokens=5000,
    )
    assert explicit.reasoning_effort == "low"
    assert explicit.thinking_budget_tokens == 5000


def test_narrow_policy_allows_partial_overlay_and_rejects_widening():
    glob = CollectionPolicy()
    narrowed = narrow_collection_policy(
        {"enabled": True, "budget": {"max_jobs_per_run": 2}},
        global_policy=glob,
        source="profile build",
    )
    assert narrowed.enabled is True
    assert narrowed.budget.max_jobs_per_run == 2
    # Untouched budgets inherit the global ceiling instead of tripping on defaults.
    assert narrowed.budget.max_parallel_jobs == glob.budget.max_parallel_jobs
    with pytest.raises(ConfigError, match="exceeds global ceiling"):
        narrow_collection_policy(
            {"budget": {"max_jobs_per_run": glob.budget.max_jobs_per_run + 1}},
            global_policy=glob,
            source="project settings",
        )
    with pytest.raises(ConfigError, match="must be a mapping"):
        narrow_collection_policy("enabled", global_policy=glob, source="t")
