"""Shared agent and trusted runtime setup for every launch entry point."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Mapping

from garuda.agents.loader import AgentProfile, load_profile, resolve_system_prompt
from garuda.core.modes import apply_mode_preset
from garuda.core.permissions import PermissionEngine
from garuda.core.rigorous import create_agent
from garuda.mcp.config import resolve_mcp_config_paths
from garuda.model.config import (
    CollectionPolicy,
    ConfigError,
    ModelBindings,
    ResolvedField,
    narrow_collection_policy,
    resolve_model_bindings,
    with_compat_reasoning_settings,
)
from garuda.model.factory import ModelFactory, ResolvedModels, safe_model_identity
from garuda.model.protocol import DEFAULT_MODEL
from garuda.runtime.router import (
    RoutingCandidate,
    RoutingDecision,
    RoutingRequest,
    candidates_from_discovered,
    record_routing_decision,
    route,
)
from garuda.tools import build_toolkit
from garuda.tools.protocol import Tool
from garuda.types import AgentConfig

logger = logging.getLogger(__name__)


def _is_sdk_model(candidate: Any) -> bool:
    """True when the caller supplied a live `Model` object rather than a name."""
    return isinstance(getattr(candidate, "model_name", None), str) and callable(
        getattr(candidate, "complete", None)
    )


def coerce_reasoning_flag(model: str | Any | None, reasoning_model: str | Any | None) -> Any | None:
    """Merge the legacy `--model` alias with `--reasoning-model`.

    `--model` remains the explicit reasoning alias. When both flags name
    different string models the request is ambiguous and fails closed with an
    actionable error; identical values (or one side unset) resolve to one.
    Live `Model` objects compare by identity.
    """
    if model is None:
        return reasoning_model
    if reasoning_model is None:
        return model
    if _is_sdk_model(model) or _is_sdk_model(reasoning_model):
        if model is reasoning_model:
            return model
        raise ConfigError(
            "conflicting model flags: --model and --reasoning-model name different models"
        )
    if model != reasoning_model:
        raise ConfigError(
            f"conflicting model flags: --model ({model!r}) and "
            f"--reasoning-model ({reasoning_model!r}) differ; pass one"
        )
    return reasoning_model


def _explicit_string(value: str | Any | None) -> str | None:
    """An explicit model *name*, or None when unset or a live SDK object."""
    if value is None or _is_sdk_model(value):
        return None
    if not isinstance(value, str) or not value.strip():
        raise ConfigError("model flags must be non-empty model names or Model objects")
    return value


@dataclass
class PreparedNativeRun:
    """Everything one native run needs, resolved once through shared setup.

    `profile`, `config`, `permissions`, `tools`, `agent`, and `mcp_manager`
    are the historical run dependencies. `bindings` is the resolved
    reasoning/collection mapping (collection is None on single-model runs, so
    the tool schema and call path stay unchanged), `resolved` holds the built
    clients, `provenance` reports where each role resolved from, and
    `collection_policy` carries the immutable collection setup — the live
    coordinator is constructed later, once the environment and parent context
    exist. Exactly two configurable model slots: reasoning and collection.

    Iterable as the historical 6-tuple so existing unpack sites keep working.
    """

    profile: AgentProfile
    config: AgentConfig
    permissions: PermissionEngine
    tools: list
    agent: object
    mcp_manager: object | None
    bindings: ModelBindings
    resolved: ResolvedModels
    provenance: dict[str, ResolvedField]
    collection_policy: CollectionPolicy

    def __iter__(self) -> Iterator[Any]:
        yield self.profile
        yield self.config
        yield self.permissions
        yield self.tools
        yield self.agent
        yield self.mcp_manager

    def __len__(self) -> int:
        return 6

    @property
    def reasoning(self) -> Any:
        """The reasoning client (owns the controller loop)."""
        return self.resolved.reasoning

    @property
    def collection(self) -> Any | None:
        """The collection client, or None on single-model runs."""
        return self.resolved.collection


def _profile_binding_alias(profile: AgentProfile) -> str | None:
    alias = profile.model_binding
    if alias is None:
        return None
    if not isinstance(alias, str) or not alias.strip():
        raise ConfigError(
            f"profile {profile.name!r}: model_binding must be a model_bindings alias string"
        )
    return alias


@dataclass(frozen=True)
class RuntimeCatalog:
    """The one trusted registry for a run, with discovery available on demand.

    Global settings authorize executable manifests and disablement. Project
    settings may only name aliases and offer disablement suggestions; neither
    can authorize a command or override the global disabled set.

    Building a catalog executes nothing: selection resolves configuration
    only. Probes run solely when a list/inspect caller asks for `discover()`.
    """

    registry: object
    project_disabled: frozenset[str] = frozenset()
    #: Malformed *advisory* project settings that were ignored, never applied.
    warnings: tuple[str, ...] = ()

    def discover(self) -> tuple[object, ...]:
        """Run the declared version/auth probes. For list/inspect, not launch."""
        from garuda.acp.catalog import discover

        return tuple(
            discover(
                self.registry.manifests,
                disabled=self.registry.disabled_ids,
                project_disabled=self.project_disabled,
            )
        )

    def select(self, ref: str):
        """Resolve a launch selection through the global disablement gate."""
        return self.registry.get(ref)

    def select_for_native_facade(self, ref: str):
        """Resolve a launchable selection before the ACP facade exists.

        The registry is already authoritative for all configured ids. This
        second check prevents a configured-but-not-yet-supported ACP command
        from being presented as selected and then silently running native.
        """
        selected = self.select(ref)
        if selected.kind.value != "native":
            from garuda.runtime.registry import RegistryError

            raise RegistryError(
                f"runtime {selected.runtime_id!r} is selected but is not launchable "
                "by this runtime facade yet"
            )
        return selected


def _advisory_project_disabled(raw: object, warnings: list[str]) -> frozenset[str]:
    """Project disablement is a suggestion; a malformed one is ignored loudly."""
    if raw is None:
        return frozenset()
    if not isinstance(raw, list) or any(not isinstance(item, str) or not item for item in raw):
        warnings.append(
            "ignored malformed project disabled_runtimes (must be a list of runtime id "
            "strings); it is advisory only"
        )
        return frozenset()
    return frozenset(raw)


def _project_runtime_refs(raw: object, *, source: str, base, warnings: list[str]) -> list:
    """Parse project aliases: refuse authority attempts, ignore malformed advice.

    A project `command` or a capability widening is an attempt to authorize
    something the trusted global settings did not, so it fails closed. Any
    other malformation (wrong shape, unknown id, duplicate alias) only names a
    convenience alias; it is dropped with a warning instead of blocking every
    run in the repository.
    """
    from garuda.runtime.registry import RegistryError, parse_project_refs

    if raw is None:
        return []
    if not isinstance(raw, list):
        warnings.append(f"ignored {source}: must be a list of references")
        return []
    for index, item in enumerate(raw):
        if isinstance(item, dict) and "command" in item:
            raise RegistryError(
                f"{source}[{index}]: project configuration cannot authorize an executable"
            )
    manifests = {manifest.runtime_id: manifest for manifest in base.manifests}
    accepted: list = []
    aliases: set[str] = set()
    for index, item in enumerate(raw):
        where = f"{source}[{index}]"
        try:
            (ref,) = parse_project_refs([item], source=where)
        except RegistryError as exc:
            warnings.append(f"ignored {exc}")
            continue
        manifest = manifests.get(ref.runtime_id)
        if manifest is None:
            warnings.append(f"ignored {where}: unknown runtime {ref.runtime_id!r}")
            continue
        if ref.capabilities is not None and not ref.capabilities <= manifest.capabilities.names:
            raise RegistryError(
                f"{where}: project alias {ref.alias!r} widens capabilities beyond "
                f"{ref.runtime_id!r}: {sorted(ref.capabilities - manifest.capabilities.names)}"
            )
        if ref.alias in aliases or ref.alias in manifests:
            warnings.append(f"ignored {where}: duplicate runtime alias {ref.alias!r}")
            continue
        aliases.add(ref.alias)
        accepted.append(ref)
    return accepted


def _trusted_manifests(global_settings, *, include_builtins: bool = True) -> list:
    """Shipped harness manifests plus the user's global ones.

    Shipped manifests are package configuration, trusted like the code that
    ships them. A global `runtimes:` entry with the same id replaces the
    shipped one: the user's file stays the anchor for what may launch.
    Duplicates *within* the global list still fail closed.
    """
    from garuda.acp.catalog import builtin_manifest_dicts
    from garuda.runtime.registry import parse_global_manifests

    shipped = (
        parse_global_manifests(builtin_manifest_dicts(), source="shipped harness manifests")
        if include_builtins
        else []
    )
    configured = parse_global_manifests(
        global_settings.get("runtimes"), source="trusted global runtimes"
    )
    overridden = {manifest.runtime_id for manifest in configured}
    return [m for m in shipped if m.runtime_id not in overridden] + configured


def build_runtime_catalog(
    *,
    global_settings,
    project_settings=None,
    disabled: frozenset[str] | set[str] | None = None,
    include_builtins: bool = True,
    source: str = "project runtime refs",
) -> RuntimeCatalog:
    """The one runtime-registry builder, for every launch, list, and inspect path.

    `global_settings` is the trusted mapping (manifests under `runtimes:`,
    `disabled_runtimes`); `project_settings` may only contribute `runtime_refs`
    aliases and advisory `disabled_runtimes`. `disabled` overrides the global
    set only for callers that already loaded it from the same trust anchor.
    Nothing is executed here.
    """
    import logging

    from garuda.acp.catalog import load_trusted_disabled
    from garuda.runtime.registry import RuntimeRegistry

    project_settings = project_settings or {}
    manifests = _trusted_manifests(global_settings, include_builtins=include_builtins)
    if disabled is None:
        disabled = load_trusted_disabled(global_settings)
    base = RuntimeRegistry(manifests, disabled=disabled)
    warnings: list[str] = []
    project_refs = _project_runtime_refs(
        project_settings.get("runtime_refs"), source=source, base=base, warnings=warnings
    )
    project_disabled = _advisory_project_disabled(
        project_settings.get("disabled_runtimes"), warnings
    )
    for warning in warnings:
        logging.getLogger(__name__).warning("%s", warning)
    return RuntimeCatalog(
        registry=RuntimeRegistry(manifests, project_refs, disabled=disabled),
        project_disabled=project_disabled,
        warnings=tuple(warnings),
    )


def prepare_runtime_catalog(workspace: str | Path) -> RuntimeCatalog:
    """Build the shared trusted runtime boundary for a workspace.

    This deliberately reads the global file through the strict runtime loader,
    rather than ``AgentHome.global_settings``: malformed global YAML must not
    erase a user's safety disablement and silently authorize a launch.
    Nothing is executed here — discovery probes run only via
    `RuntimeCatalog.discover()`.
    """
    from garuda.acp.catalog import load_trusted_runtime_settings
    from garuda.config.agent_home import resolve_agent_home

    global_settings = load_trusted_runtime_settings()
    home = resolve_agent_home(workspace)
    return build_runtime_catalog(
        global_settings=global_settings,
        project_settings=home.settings,
        source=f"project runtime refs ({home.workspace})",
    )


def build_routing_candidates(
    workspace: str,
    *,
    costs: Mapping[str, float | None] | None = None,
    history: Mapping[str, tuple[int, int]] | None = None,
    mutating_allowed: Mapping[str, bool] | None = None,
    disabled=None,
    global_settings: dict | None = None,
    project_settings: dict | None = None,
) -> list[RoutingCandidate]:
    """Build routing candidates from the shared registry and discovery.

    Every product path that needs policy ranking goes through here so
    disabled ids surface as unavailable and duplicates refuse at registry
    construction — never a hand-rolled manifest list.
    """
    from garuda.acp.catalog import (
        discover,
        load_trusted_disabled,
        load_trusted_runtime_settings,
        shared_registry,
    )
    from garuda.config.agent_home import resolve_agent_home

    home = resolve_agent_home(workspace)
    if global_settings is None:
        global_settings = load_trusted_runtime_settings()
    if project_settings is None:
        project_settings = getattr(home, "settings", None) or {}
    if disabled is None:
        disabled = load_trusted_disabled(global_settings)
    extra_manifests = global_settings.get("runtimes", [])
    project_refs = project_settings.get("runtime_refs", [])
    project_disabled = project_settings.get("disabled_runtimes", []) or []
    registry = shared_registry(
        extra_manifests=extra_manifests,
        project_refs=project_refs,
        disabled=disabled,
    )
    discovered = discover(
        registry.manifests,
        disabled=registry.disabled_ids,
        project_disabled=frozenset(project_disabled),
    )
    return candidates_from_discovered(
        discovered,
        costs=costs,
        history=history,
        mutating_allowed=mutating_allowed,
    )


def route_session(
    store,
    session_id: str,
    *,
    workspace: str,
    request: RoutingRequest,
    costs: Mapping[str, float | None] | None = None,
    history: Mapping[str, tuple[int, int]] | None = None,
    mutating_allowed: Mapping[str, bool] | None = None,
    disabled=None,
    global_settings: dict | None = None,
    project_settings: dict | None = None,
    persist: bool = True,
) -> RoutingDecision:
    """Rank configured runtimes and optionally persist the decision.

    Call after ``SessionStore.begin`` and before any runtime ``start`` so a
    refused pin/budget never launches, and a successful decision is already
    on the unified session meta when the adapter begins.
    """
    candidates = build_routing_candidates(
        workspace,
        costs=costs,
        history=history,
        mutating_allowed=mutating_allowed,
        disabled=disabled,
        global_settings=global_settings,
        project_settings=project_settings,
    )
    decision = route(candidates, request)
    if persist:
        record_routing_decision(store, session_id, decision)
    return decision


def select_runtime(
    workspace: str,
    requested: str,
    *,
    required_capabilities: tuple[str, ...] = (),
    budget_usd: float | None = None,
    mutating: bool = True,
) -> str:
    """Select the executor before provider/model construction.

    An explicit non-native runtime remains pinned. The native default is an
    automatic policy request when trusted routing is enabled, so the selected
    id—not the default—drives the actual entry point.
    """
    from garuda.config.agent_home import resolve_agent_home

    home = resolve_agent_home(workspace)
    global_settings = getattr(home, "global_settings", None) or {}
    routing_cfg = global_settings.get("routing") or {}
    if not isinstance(routing_cfg, dict) or not routing_cfg.get("enabled", False):
        return requested
    if budget_usd is None and routing_cfg.get("budget_usd") is not None:
        budget_usd = float(routing_cfg["budget_usd"])
    request = RoutingRequest(
        required_capabilities=required_capabilities,
        pin=None if requested == "native" else requested,
        budget_usd=budget_usd,
        mutating=mutating,
    )
    return route(
        build_routing_candidates(workspace, global_settings=global_settings), request
    ).selected


def resolve_and_record_routing(
    *,
    workspace: str,
    store: object,
    session_id: str,
    pin: str | None = None,
    budget_usd: float | None = None,
    mutating: bool = True,
    required_capabilities: tuple[str, ...] = (),
    enabled: bool | None = None,
    costs: Mapping[str, float | None] | None = None,
    history: Mapping[str, tuple[int, int]] | None = None,
    mutating_allowed: Mapping[str, bool] | None = None,
) -> RoutingDecision | None:
    """Discover, route, and persist through the shared product wiring boundary.

    Runtime policy remains provider-neutral. Provider discovery and trusted
    configuration belong here, outside :mod:`garuda.runtime`, and the decision
    is persisted before callers start a runtime.
    """
    from garuda.config.agent_home import resolve_agent_home

    home = resolve_agent_home(workspace)
    global_settings = getattr(home, "global_settings", None) or {}
    project_settings = getattr(home, "settings", None) or {}
    routing_cfg = global_settings.get("routing") or {}
    if enabled is None:
        enabled = (
            bool(routing_cfg.get("enabled", False))
            if isinstance(routing_cfg, dict)
            else False
        )
    if not enabled:
        return None
    if budget_usd is None and isinstance(routing_cfg, dict):
        raw_budget = routing_cfg.get("budget_usd")
        if raw_budget is not None:
            budget_usd = float(raw_budget)

    return route_session(
        store,
        session_id,
        workspace=workspace,
        request=RoutingRequest(
            required_capabilities=required_capabilities,
            pin=pin,
            budget_usd=budget_usd,
            mutating=mutating,
        ),
        costs=costs,
        history=history,
        mutating_allowed=mutating_allowed,
        global_settings=global_settings,
        project_settings=project_settings,
    )


async def prepare_agent_run(
    agent_name: str,
    *,
    workspace: str,
    agents_dir: Path | list[Path] | None = None,
    mcp_config_path: str | None = None,
    mode: str | None = None,
    permission_mode: str | None = None,
    approval_handler=None,
    extra_tools: list[Tool] | None = None,
    load_project_tools: bool | None = None,
    # Dual-model role bindings (Tasks 1-3). `model` is the legacy explicit
    # reasoning alias; `reasoning_model` is its new spelling. Strings are model
    # names resolved through the shared precedence chain; live `Model` objects
    # are SDK-supplied clients kept by identity for their run only.
    model: str | Any | None = None,
    reasoning_model: str | Any | None = None,
    collection_model: str | Any | None = None,
    no_collection: bool = False,
    model_binding: str | None = None,
    reasoning_effort: str | None = None,
    thinking_budget_tokens: int | None = None,
) -> PreparedNativeRun:
    """Load profile, resolve model bindings, build toolkit, return run dependencies.

    ``permission_mode`` overrides both the profile's declaration and the mode preset.
    It has to be applied here rather than by the caller because ``PermissionEngine`` takes
    its mode at construction and exposes no setter — a caller that assigned
    ``config.permission_mode`` afterwards would change the reported posture while the
    engine kept enforcing the old one, which is the worst of the three outcomes.

    Model precedence per role: explicit args > role env > legacy GARUDA_MODEL
    (reasoning) > route binding > profile > project > global > built-in. Omitted
    flags stay None so lower-precedence configuration is never masked by an
    eager parser default. Prepared clients are built fresh per call: concurrent
    server jobs never share them.
    """
    from garuda.config.agent_home import resolve_agent_home, resolve_agents_dirs

    # Default the profiles dirs to the project's `.agent/agents` then `.garuda/agents`
    # when the caller didn't pass any. Idempotent: an explicit dir/list is kept as-is.
    agents_dirs = resolve_agents_dirs(workspace, agents_dir)
    profile = load_profile(agent_name, extra_dir=agents_dirs)
    config = profile.to_agent_config()
    # Only override the profile's own mode when a caller explicitly asked for one,
    # so a `mode: rigorous` profile isn't silently downgraded.
    if mode:
        config.mode = mode
    # The mode decides the gate posture; fields the profile declared explicitly are
    # left alone. Every entry point funnels through here, so this is the one place
    # a preset needs applying.
    apply_mode_preset(config, declared_fields=profile.declared_fields)
    # After the preset, so an explicit request is the narrower statement of intent and
    # wins — the same ordering the CLI uses for its own flags.
    if permission_mode:
        config.permission_mode = permission_mode
    # Legacy reasoning knobs from CLI flags narrow the profile's own before the
    # compatibility translation below turns them into the reasoning ModelSpec.
    if reasoning_effort is not None:
        config.reasoning_effort = reasoning_effort
    if thinking_budget_tokens is not None:
        config.thinking_budget_tokens = thinking_budget_tokens
    config.system_prompt = resolve_system_prompt(profile, workspace)

    # --- Model bindings -----------------------------------------------------
    from garuda.config.routing import load_global_orchestration, project_orchestration_from_home

    home = resolve_agent_home(workspace)
    global_orch = load_global_orchestration()
    try:
        project_orch = project_orchestration_from_home(home, global_orch)
    except ConfigError:
        raise
    except Exception as exc:
        raise ConfigError(f"project settings: cannot parse orchestration: {exc}") from exc

    if model_binding is not None and model_binding not in global_orch.model_bindings:
        raise ConfigError(
            f"explicit model_binding: unknown alias {model_binding!r} "
            f"(known: {sorted(global_orch.model_bindings)})"
        )
    route_binding = global_orch.model_bindings.get(model_binding) if model_binding else None

    profile_alias = _profile_binding_alias(profile)
    profile_binding: ModelBindings | None = None
    if profile_alias is not None:
        if profile_alias not in global_orch.model_bindings:
            raise ConfigError(
                f"profile {profile.name!r}: unknown model_binding alias {profile_alias!r} "
                f"(known: {sorted(global_orch.model_bindings)})"
            )
        profile_binding = global_orch.model_bindings[profile_alias]

    project_binding: ModelBindings | None = None
    if project_orch.model_binding is not None:
        project_binding = global_orch.model_bindings[project_orch.model_binding]

    default_binding: ModelBindings | None = None
    if global_orch.model_bindings:
        default_binding = global_orch.model_bindings.get(global_orch.default_binding)
        if default_binding is None and len(global_orch.model_bindings) == 1:
            default_binding = next(iter(global_orch.model_bindings.values()))

    merged_reasoning = coerce_reasoning_flag(model, reasoning_model)
    sdk_reasoning = merged_reasoning if _is_sdk_model(merged_reasoning) else None
    sdk_collection = collection_model if _is_sdk_model(collection_model) else None
    explicit_reasoning = _explicit_string(merged_reasoning)
    explicit_collection = _explicit_string(collection_model)

    # A bare SDK/CLI default equal to the built-in is "unspecified", not
    # explicit: otherwise the default would mask env, profile, project, and
    # global bindings below it. An explicit identical value resolves the same.
    if explicit_reasoning == DEFAULT_MODEL:
        explicit_reasoning = None
    if explicit_collection == DEFAULT_MODEL:
        explicit_collection = None

    bindings, provenance = resolve_model_bindings(
        explicit_reasoning=explicit_reasoning,
        explicit_collection=explicit_collection,
        no_collection=no_collection,
        route_binding=route_binding,
        profile_binding=profile_binding,
        project_binding=project_binding,
        global_binding=default_binding,
    )

    # Compatibility period: legacy profile/config reasoning knobs become the
    # reasoning spec's unset fields. The spec wins where it speaks.
    bindings = ModelBindings(
        reasoning=with_compat_reasoning_settings(
            bindings.reasoning,
            reasoning_effort=config.reasoning_effort,
            thinking_budget_tokens=config.thinking_budget_tokens,
        ),
        collection=bindings.collection,
    )

    # Effective collection policy: global authorizes, profile/project narrow.
    # Numeric budgets may only shrink; toggles are restated per source.
    collection_policy = global_orch.collection
    if profile.collection is not None:
        collection_policy = narrow_collection_policy(
            profile.collection,
            global_policy=collection_policy,
            source=f"profile {profile.name!r}",
        )
    project_raw_collection = (
        home.settings.get("collection") if isinstance(home.settings, dict) else None
    )
    if project_raw_collection is not None:
        collection_policy = narrow_collection_policy(
            project_raw_collection,
            global_policy=collection_policy,
            source=f"project settings ({home.workspace})",
        )

    resolved = ModelFactory().build(
        bindings,
        provenance,
        reasoning_model=sdk_reasoning,
        collection_model=sdk_collection,
    )

    logger.info(
        "Resolved models: reasoning=%s (%s), collection=%s (%s)",
        safe_model_identity(resolved.reasoning),
        provenance["reasoning"].provenance.value,
        safe_model_identity(resolved.collection) if resolved.collection is not None else "none",
        provenance["collection"].provenance.value,
    )

    # --- Toolkit ------------------------------------------------------------
    mcp_paths = resolve_mcp_config_paths(workspace, mcp_config_path or config.mcp_config_path)
    permissions = PermissionEngine(
        mode=config.permission_mode,
        tool_rules=profile.tool_rules,
        path_rules=profile.path_rules,
        bash_rules=profile.bash_rules,
        approval_handler=approval_handler,
    )
    tools, mcp_manager = await build_toolkit(
        profile.tools,
        mcp_paths,
        extra_tools=extra_tools,
        workspace=workspace,
        load_project_tools=load_project_tools,
        mcp_servers=profile.mcp_servers,
    )
    agent = create_agent(profile.name, mode=config.mode)
    return PreparedNativeRun(
        profile=profile,
        config=config,
        permissions=permissions,
        tools=tools,
        agent=agent,
        mcp_manager=mcp_manager,
        bindings=bindings,
        resolved=resolved,
        provenance=provenance,
        collection_policy=collection_policy,
    )
