"""Model-role configuration: specs, bindings, budgets, and trust rules.

Two configurable slots exist: ``reasoning`` (the controller) and ``collection``
(the bounded evidence gatherer). Verifier, summarizer, planner, critic,
controller, collector, and classifier are call *purposes* for accounting — not
extra bindings.

Trust rule (fail closed): global configuration authorizes providers, API bases,
and ceilings. Project settings and profiles may only reference authorized
aliases and narrow limits — never widen them or introduce new providers,
endpoints, executables, or credential sources.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import Enum

from garuda.model.protocol import DEFAULT_MODEL, MODEL_ENV_VAR

REASONING_ENV_VAR = "GARUDA_REASONING_MODEL"
COLLECTION_ENV_VAR = "GARUDA_COLLECTION_MODEL"

BUILTIN_REASONING_MODEL = DEFAULT_MODEL

SUPPORTED_TRANSPORTS = frozenset({"litellm"})
SUPPORTED_FALLBACKS = frozenset({"reasoning", "fail"})
SUPPORTED_HANDOFFS = frozenset({"none", "brief"})


class ConfigError(ValueError):
    """Source-qualified configuration failure (path + field included)."""


class Provenance(str, Enum):
    EXPLICIT = "explicit"
    ENV = "env"
    LEGACY_ENV = "legacy_env"
    ROUTE = "route"
    PROFILE = "profile"
    PROJECT = "project"
    GLOBAL = "global"
    BUILTIN = "builtin"


@dataclass(frozen=True)
class ModelSpec:
    transport: str = "litellm"
    model: str = BUILTIN_REASONING_MODEL
    api_base: str | None = None
    reasoning_effort: str | None = None
    thinking_budget_tokens: int | None = None
    max_tokens: int | None = None
    timeout_sec: float | None = None

    def validate(self, *, source: str = "") -> None:
        prefix = f"{source}: " if source else ""
        if self.transport not in SUPPORTED_TRANSPORTS:
            raise ConfigError(f"{prefix}unsupported transport {self.transport!r}")
        if not self.model or not isinstance(self.model, str):
            raise ConfigError(f"{prefix}model must be a non-empty string")
        for label, value in (
            ("thinking_budget_tokens", self.thinking_budget_tokens),
            ("max_tokens", self.max_tokens),
            ("timeout_sec", self.timeout_sec),
        ):
            if value is not None and (not isinstance(value, (int, float)) or value <= 0):
                raise ConfigError(f"{prefix}{label} must be a positive number")


@dataclass(frozen=True)
class ModelBindings:
    reasoning: ModelSpec = field(default_factory=ModelSpec)
    collection: ModelSpec | None = None


@dataclass(frozen=True)
class CollectionBudget:
    max_jobs_per_run: int = 8
    max_parallel_jobs: int = 3
    max_turns_per_job: int = 12
    max_tokens_per_job: int = 30_000
    max_total_tokens_per_run: int | None = None
    max_cost_usd_per_run: float | None = None
    deadline_fraction: float = 0.5


@dataclass(frozen=True)
class CollectionFallback:
    interactive: str = "reasoning"
    readonly: str = "reasoning"
    eval: str = "fail"
    rigorous: str = "fail"

    def for_mode(self, mode: str) -> str:
        return getattr(self, mode, "fail")


@dataclass(frozen=True)
class CollectionPolicy:
    enabled: bool = False
    profile: str = "explore"
    handoff: str = "brief"
    budget: CollectionBudget = field(default_factory=CollectionBudget)
    fallback: CollectionFallback = field(default_factory=CollectionFallback)


@dataclass(frozen=True)
class ResolvedField:
    """A resolved value plus where it came from (for explainability)."""

    value: object
    provenance: Provenance
    source: str = ""


def parse_model_spec(data: object, *, source: str) -> ModelSpec:
    """Parse one model alias body; fail closed with a source-qualified error."""
    if not isinstance(data, dict):
        raise ConfigError(f"{source}: model spec must be a mapping")
    if "api_key" in data:
        raise ConfigError(
            f"{source}: model specs must not store an API key; "
            "use provider-supported environment variables or explicit caller arguments"
        )
    transport = data.get("transport", "litellm")
    model = data.get("model", "")
    spec = ModelSpec(
        transport=transport,
        model=model,
        api_base=data.get("api_base"),
        reasoning_effort=data.get("reasoning_effort"),
        thinking_budget_tokens=data.get("thinking_budget_tokens"),
        max_tokens=data.get("max_tokens"),
        timeout_sec=data.get("timeout_sec"),
    )
    spec.validate(source=source)
    unknown = set(data) - {
        "transport", "model", "api_base", "reasoning_effort",
        "thinking_budget_tokens", "max_tokens", "timeout_sec",
    }
    if unknown:
        raise ConfigError(f"{source}: unknown model fields: {sorted(unknown)}")
    return spec


def with_compat_reasoning_settings(
    spec: ModelSpec,
    *,
    reasoning_effort: str | None = None,
    thinking_budget_tokens: int | None = None,
) -> ModelSpec:
    """Translate legacy profile/config reasoning knobs into a reasoning spec.

    Compatibility period: existing `reasoning_effort` / `thinking_budget_tokens`
    on `AgentProfile` and `AgentConfig` predate role-specific model settings.
    Explicit spec fields win; legacy knobs only fill gaps they leave unset.
    Returns a new spec (specs are immutable).
    """
    effort = spec.reasoning_effort if spec.reasoning_effort is not None else reasoning_effort
    budget = (
        spec.thinking_budget_tokens
        if spec.thinking_budget_tokens is not None
        else thinking_budget_tokens
    )
    if effort == spec.reasoning_effort and budget == spec.thinking_budget_tokens:
        return spec
    return ModelSpec(
        transport=spec.transport,
        model=spec.model,
        api_base=spec.api_base,
        reasoning_effort=effort,
        thinking_budget_tokens=budget,
        max_tokens=spec.max_tokens,
        timeout_sec=spec.timeout_sec,
    )


def narrow_collection_policy(
    data: object, *, global_policy: CollectionPolicy, source: str
) -> CollectionPolicy:
    """Merge a profile/project collection block over the global policy.

    Toggles (``enabled``, ``profile``, ``handoff``, ``fallback``) may be
    restated per profile or project; numeric budgets may only narrow the global
    ceiling — only keys the overlay actually sets are compared, so a partial
    overlay never trips on defaults it did not ask for. Anything wider, unknown,
    or malformed fails closed with a source-qualified error.
    """
    if not isinstance(data, dict):
        raise ConfigError(f"{source}: collection must be a mapping")
    raw_budget = data.get("budget", {}) or {}
    if not isinstance(raw_budget, dict):
        raise ConfigError(f"{source}: collection.budget must be a mapping")
    global_budget = global_policy.budget
    known_budget_fields = (
        "max_jobs_per_run",
        "max_parallel_jobs",
        "max_turns_per_job",
        "max_tokens_per_job",
        "max_total_tokens_per_run",
        "max_cost_usd_per_run",
        "deadline_fraction",
    )
    for key, value in raw_budget.items():
        if key not in known_budget_fields:
            raise ConfigError(f"{source}: collection.budget.{key} is not a known budget field")
        ceiling = getattr(global_budget, key, None)
        if value is not None and ceiling is not None and value > ceiling:
            raise ConfigError(
                f"{source}: collection.budget.{key}={value} exceeds global ceiling {ceiling}"
            )
    merged_budget = {
        key: raw_budget.get(key, getattr(global_budget, key)) for key in known_budget_fields
    }
    merged = {
        "enabled": data.get("enabled", global_policy.enabled),
        "profile": data.get("profile", global_policy.profile),
        "handoff": data.get("handoff", global_policy.handoff),
        "budget": merged_budget,
        "fallback": data.get(
            "fallback",
            {
                "interactive": global_policy.fallback.interactive,
                "readonly": global_policy.fallback.readonly,
                "eval": global_policy.fallback.eval,
                "rigorous": global_policy.fallback.rigorous,
            },
        ),
    }
    return parse_collection_policy(merged, source=source)


def _provider_of(model_name: str) -> str:
    return model_name.split("/", 1)[0] if "/" in model_name else model_name


def resolve_model_bindings(
    *,
    explicit_reasoning: str | None = None,
    explicit_collection: str | None = None,
    no_collection: bool = False,
    route_binding: ModelBindings | None = None,
    profile_binding: ModelBindings | None = None,
    project_binding: ModelBindings | None = None,
    global_binding: ModelBindings | None = None,
    global_models: dict[str, ModelSpec] | None = None,
    env: dict[str, str] | None = None,
) -> tuple[ModelBindings, dict[str, ResolvedField]]:
    """Resolve reasoning/collection bindings per the approved precedence.

    Order: explicit args > role env > legacy GARUDA_MODEL (reasoning) >
    route > profile > project > global > built-in. An absent collection model
    yields ``None`` so single-model runs keep today's tool schema.
    """
    env = env if env is not None else dict(os.environ)
    provenance: dict[str, ResolvedField] = {}

    def pick(
        role: str,
        explicit: str | None,
        role_env: str | None,
        legacy: str | None,
    ) -> tuple[ModelSpec | None, ResolvedField]:
        if explicit:
            spec = ModelSpec(model=explicit)
            return spec, ResolvedField(spec, Provenance.EXPLICIT, "cli")
        if role_env:
            spec = ModelSpec(model=role_env)
            return spec, ResolvedField(spec, Provenance.ENV, REASONING_ENV_VAR if role == "reasoning" else COLLECTION_ENV_VAR)
        if role == "reasoning" and legacy:
            spec = ModelSpec(model=legacy)
            return spec, ResolvedField(spec, Provenance.LEGACY_ENV, MODEL_ENV_VAR)
        for bindings, prov in (
            (route_binding, Provenance.ROUTE),
            (profile_binding, Provenance.PROFILE),
            (project_binding, Provenance.PROJECT),
            (global_binding, Provenance.GLOBAL),
        ):
            if bindings is not None:
                spec = bindings.reasoning if role == "reasoning" else bindings.collection
                if spec is not None:
                    return spec, ResolvedField(spec, prov, prov.value)
        if role == "reasoning":
            spec = ModelSpec()
            return spec, ResolvedField(spec, Provenance.BUILTIN, "builtin")
        return None, ResolvedField(None, Provenance.BUILTIN, "builtin")

    reasoning, reasoning_field = pick(
        "reasoning", explicit_reasoning,
        (env.get(REASONING_ENV_VAR) or None),
        (env.get(MODEL_ENV_VAR) or None),
    )
    assert reasoning is not None
    provenance["reasoning"] = reasoning_field

    if no_collection:
        provenance["collection"] = ResolvedField(None, Provenance.EXPLICIT, "--no-collection")
        return ModelBindings(reasoning=reasoning, collection=None), provenance

    collection, collection_field = pick(
        "collection", explicit_collection, (env.get(COLLECTION_ENV_VAR) or None), None,
    )
    provenance["collection"] = collection_field
    return ModelBindings(reasoning=reasoning, collection=collection), provenance


def check_project_trust(
    *,
    project_models: dict[str, ModelSpec],
    global_models: dict[str, ModelSpec],
    project_budget: CollectionBudget | None = None,
    global_budget: CollectionBudget | None = None,
    source: str = "project settings",
) -> None:
    """Enforce that project configuration narrows but never widens trust.

    - Project may not introduce providers/models not authorized globally.
    - Project may not set api_base (changes where content is sent).
    - Project budgets may not exceed global ceilings.
    """
    authorized = {_provider_of(spec.model) for spec in global_models.values()}
    authorized |= {spec.model for spec in global_models.values()}
    for alias, spec in project_models.items():
        if spec.api_base:
            raise ConfigError(f"{source}: alias {alias!r} must not set api_base")
        if spec.model not in authorized and _provider_of(spec.model) not in authorized:
            raise ConfigError(
                f"{source}: alias {alias!r} model {spec.model!r} is not authorized globally"
            )
    if project_budget is not None and global_budget is not None:
        for fname in (
            "max_jobs_per_run", "max_parallel_jobs", "max_turns_per_job",
            "max_tokens_per_job", "max_total_tokens_per_run",
            "max_cost_usd_per_run",
        ):
            pval = getattr(project_budget, fname)
            gval = getattr(global_budget, fname)
            if pval is not None and gval is not None and pval > gval:
                raise ConfigError(
                    f"{source}: budget {fname}={pval} exceeds global ceiling {gval}"
                )


def parse_collection_policy(data: object, *, source: str) -> CollectionPolicy:
    """Parse a collection policy block; unknown modes/values fail closed."""
    if data is None:
        return CollectionPolicy()
    if not isinstance(data, dict):
        raise ConfigError(f"{source}: collection policy must be a mapping")
    budget_data = data.get("budget", {}) or {}
    if not isinstance(budget_data, dict):
        raise ConfigError(f"{source}: collection.budget must be a mapping")
    try:
        budget = CollectionBudget(**{k: v for k, v in budget_data.items() if k in CollectionBudget.__dataclass_fields__})
    except TypeError as exc:
        raise ConfigError(f"{source}: invalid collection.budget: {exc}") from exc
    for fname in (
        "max_jobs_per_run", "max_parallel_jobs", "max_turns_per_job", "max_tokens_per_job",
    ):
        if getattr(budget, fname) is not None and getattr(budget, fname) <= 0:
            raise ConfigError(f"{source}: collection.budget.{fname} must be positive")
    fallback_data = data.get("fallback", {}) or {}
    if not isinstance(fallback_data, dict):
        raise ConfigError(f"{source}: collection.fallback must be a mapping")
    for mode, value in fallback_data.items():
        if value not in SUPPORTED_FALLBACKS:
            raise ConfigError(f"{source}: collection.fallback.{mode} must be one of {sorted(SUPPORTED_FALLBACKS)}")
    fallback = CollectionFallback(**{k: v for k, v in fallback_data.items() if k in CollectionFallback.__dataclass_fields__})
    handoff = data.get("handoff", "brief")
    if handoff not in SUPPORTED_HANDOFFS:
        raise ConfigError(f"{source}: collection.handoff must be one of {sorted(SUPPORTED_HANDOFFS)}")
    unknown = set(data) - {"enabled", "profile", "handoff", "budget", "fallback"}
    if unknown:
        raise ConfigError(f"{source}: unknown collection fields: {sorted(unknown)}")
    return CollectionPolicy(
        enabled=bool(data.get("enabled", False)),
        profile=str(data.get("profile", "explore")),
        handoff=handoff,
        budget=budget,
        fallback=fallback,
    )
