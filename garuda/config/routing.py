"""Orchestration configuration: trusted global settings vs project references.

Global (user-level ``settings.yaml``) authorizes model aliases, bindings, and
ceilings. Project (``.agent/settings.yaml``) may only reference authorized
aliases and narrow limits. The two are parsed separately so trust decisions see
which file a value came from — a shallow merge would lose exactly that.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from garuda.model.config import (
    CollectionPolicy,
    ConfigError,
    ModelBindings,
    ModelSpec,
    check_project_trust,
    parse_collection_policy,
    parse_model_spec,
)


@dataclass(frozen=True)
class GlobalOrchestration:
    models: dict[str, ModelSpec] = field(default_factory=dict)
    model_bindings: dict[str, ModelBindings] = field(default_factory=dict)
    default_binding: str = "default"
    collection: CollectionPolicy = field(default_factory=CollectionPolicy)


@dataclass(frozen=True)
class ProjectOrchestration:
    model_binding: str | None = None
    collection: CollectionPolicy | None = None


def _parse_bindings(
    data: object, models: dict[str, ModelSpec], *, source: str
) -> dict[str, ModelBindings]:
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(f"{source}: model_bindings must be a mapping")
    out: dict[str, ModelBindings] = {}
    for name, body in data.items():
        where = f"{source}.model_bindings.{name}"
        if not isinstance(body, dict):
            raise ConfigError(f"{where}: must be a mapping")
        unknown = set(body) - {"reasoning", "collection"}
        if unknown:
            raise ConfigError(f"{where}: unknown fields {sorted(unknown)}")
        reasoning_ref = body.get("reasoning")
        if not reasoning_ref or reasoning_ref not in models:
            raise ConfigError(f"{where}.reasoning: unknown model alias {reasoning_ref!r}")
        collection_ref = body.get("collection")
        collection = None
        if collection_ref is not None:
            if collection_ref not in models:
                raise ConfigError(f"{where}.collection: unknown model alias {collection_ref!r}")
            collection = models[collection_ref]
        out[str(name)] = ModelBindings(reasoning=models[reasoning_ref], collection=collection)
    return out


def parse_global_orchestration(data: object, *, source: str = "global settings") -> GlobalOrchestration:
    """Parse trusted global orchestration config."""
    if data is None:
        return GlobalOrchestration()
    if not isinstance(data, dict):
        raise ConfigError(f"{source}: must be a mapping")
    models_data = data.get("models", {}) or {}
    if not isinstance(models_data, dict):
        raise ConfigError(f"{source}.models: must be a mapping")
    models = {str(k): parse_model_spec(v, source=f"{source}.models.{k}") for k, v in models_data.items()}
    bindings = _parse_bindings(data.get("model_bindings"), models, source=source)
    collection = parse_collection_policy(data.get("collection"), source=f"{source}.collection")
    default_binding = str(data.get("model_bindings_default", "default"))
    # Unknown top-level keys are ignored: the global settings.yaml carries
    # unrelated harness settings alongside orchestration blocks.
    return GlobalOrchestration(
        models=models, model_bindings=bindings,
        default_binding=default_binding, collection=collection,
    )


def parse_project_orchestration(    data: object, global_orch: GlobalOrchestration, *, source: str = "project settings"
) -> ProjectOrchestration:
    """Parse workspace-controlled project config against trusted global config.

    Project config may reference binding aliases and narrow collection limits.
    It may not define models, endpoints, executables, credentials, or wider
    ceilings — those fail closed here, not after a merge.
    """
    if data is None:
        return ProjectOrchestration()
    if not isinstance(data, dict):
        raise ConfigError(f"{source}: must be a mapping")
    for forbidden in ("models", "model_bindings", "executables", "commands", "api_base", "credentials"):
        if forbidden in data:
            raise ConfigError(f"{source}: must not define {forbidden!r}")
    binding = data.get("model_binding")
    if binding is not None and binding not in global_orch.model_bindings:
        raise ConfigError(f"{source}.model_binding: unknown alias {binding!r}")
    collection = None
    if "collection" in data:
        collection = parse_collection_policy(data["collection"], source=f"{source}.collection")
        check_project_trust(
            project_models={}, global_models=global_orch.models,
            project_budget=collection.budget, global_budget=global_orch.collection.budget,
            source=source,
        )
    unknown = set(data) - {"model_binding", "collection"}
    if unknown:
        raise ConfigError(f"{source}: unknown fields {sorted(unknown)}")
    return ProjectOrchestration(model_binding=binding, collection=collection)


def load_global_orchestration() -> GlobalOrchestration:
    """Load trusted orchestration blocks from the user-level settings.yaml.

    Missing file or parse failure yields an empty orchestration (built-in
    defaults) — fail closed on trust-shaped ambiguity happens per-field in
    the parsers, not by refusing to run without a settings file.
    """
    import logging

    import yaml

    from garuda.config.agent_home import global_settings_path

    logger = logging.getLogger(__name__)
    path = global_settings_path()
    if not path.is_file():
        return GlobalOrchestration()
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        logger.warning("Failed to parse global settings %s", path, exc_info=True)
        return GlobalOrchestration()
    if not isinstance(data, dict):
        return GlobalOrchestration()
    return parse_global_orchestration(data, source=f"global settings ({path})")


def project_orchestration_from_home(home: object, global_orch: GlobalOrchestration) -> ProjectOrchestration:
    """Parse the workspace-controlled settings against the trusted global config."""
    settings = getattr(home, "settings", None) or {}
    if not isinstance(settings, dict):
        return ProjectOrchestration()
    relevant = {k: v for k, v in settings.items() if k in ("model_binding", "collection")}
    if not relevant:
        return ProjectOrchestration()
    workspace = getattr(home, "workspace", "?")
    return parse_project_orchestration(relevant, global_orch, source=f"project settings ({workspace})")
