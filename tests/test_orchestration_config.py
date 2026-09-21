"""Orchestration config: global authorizes, project references and narrows."""

import pytest

from garuda.config.routing import parse_global_orchestration, parse_project_orchestration
from garuda.model.config import ConfigError

GLOBAL = {
    "models": {
        "strong": {"transport": "litellm", "model": "provider-a/reasoning"},
        "collector": {"transport": "litellm", "model": "provider-b/collector", "max_tokens": 6000},
    },
    "model_bindings": {
        "default": {"reasoning": "strong", "collection": "collector"},
        "solo": {"reasoning": "strong"},
    },
    "collection": {"enabled": True, "budget": {"max_jobs_per_run": 8}},
}


def test_global_parses_aliases_and_bindings():
    orch = parse_global_orchestration(GLOBAL)
    assert orch.models["strong"].model == "provider-a/reasoning"
    assert orch.model_bindings["default"].collection is not None
    assert orch.model_bindings["solo"].collection is None


def test_global_unknown_alias_fails():
    bad = {"models": {"s": {"model": "a/m"}}, "model_bindings": {"d": {"reasoning": "nope"}}}
    with pytest.raises(ConfigError):
        parse_global_orchestration(bad)


def test_project_may_reference_and_narrow():
    orch = parse_global_orchestration(GLOBAL)
    proj = parse_project_orchestration(
        {"model_binding": "solo", "collection": {"enabled": True, "budget": {"max_jobs_per_run": 2}}},
        orch,
    )
    assert proj.model_binding == "solo"
    assert proj.collection.budget.max_jobs_per_run == 2


def test_project_cannot_define_models_or_widen():
    orch = parse_global_orchestration(GLOBAL)
    with pytest.raises(ConfigError):
        parse_project_orchestration({"models": {"x": {"model": "evil/m"}}}, orch)
    with pytest.raises(ConfigError):
        parse_project_orchestration(
            {"collection": {"budget": {"max_jobs_per_run": 99}}}, orch
        )
    with pytest.raises(ConfigError):
        parse_project_orchestration({"model_binding": "nope"}, orch)


def test_project_cannot_define_executables_or_credentials():
    orch = parse_global_orchestration(GLOBAL)
    for forbidden in ("executables", "commands", "api_base", "credentials"):
        with pytest.raises(ConfigError, match="must not define"):
            parse_project_orchestration({forbidden: {"x": "y"}}, orch)


def test_partial_project_overlay_narrows_without_tripping_on_defaults():
    orch = parse_global_orchestration(
        {**GLOBAL, "collection": {"enabled": True, "budget": {"max_tokens_per_job": 1000}}}
    )
    proj = parse_project_orchestration(
        {"collection": {"budget": {"max_jobs_per_run": 2}}}, orch
    )
    assert proj.collection.budget.max_jobs_per_run == 2
    # Untouched fields inherit the (already narrowed) global ceiling.
    assert proj.collection.budget.max_tokens_per_job == 1000
    # But restating a wider value for any field still fails closed.
    with pytest.raises(ConfigError, match="exceeds global ceiling"):
        parse_project_orchestration(
            {"collection": {"budget": {"max_tokens_per_job": 1001}}}, orch
        )
