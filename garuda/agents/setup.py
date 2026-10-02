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

    Building a catalog executes nothing. Probes run only when a list/inspect
    caller or initial-runtime selection explicitly asks for `discover()`.
    """

    registry: object
    project_disabled: frozenset[str] = frozenset()
    #: Malformed *advisory* project settings that were ignored, never applied.
    warnings: tuple[str, ...] = ()

    def discover(
        self, *, only: frozenset[str] | None = None, cache_ttl: float | None = None
    ) -> tuple[object, ...]:
        """Run the declared version/auth probes for inspection or selection.

        ``only`` limits probing to those runtime ids (selection probes just the
        runtimes that could be chosen); ``cache_ttl`` reuses a recent result.
        Listing and inspection call this with neither, so they always probe.
        """
        from garuda.acp.catalog import discover

        manifests = self.registry.manifests
        if only is not None:
            manifests = tuple(m for m in manifests if m.runtime_id in only)
            if not manifests:
                return ()
        return tuple(
            discover(
                manifests,
                disabled=self.registry.disabled_ids,
                project_disabled=self.project_disabled,
                cache_ttl=cache_ttl,
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


@dataclass(frozen=True)
class InitialRuntimeSelection:
    """Provider-bound launch facts for one initial-runtime decision.

    The generic selection package remains pure; this setup-layer value carries
    the trusted registry/discovery facts that both selection and launch use.
    """

    selection: object
    request: object
    candidates: tuple[object, ...]


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


#: How long initial selection may reuse a runtime's probe result (seconds).
SELECTION_PROBE_CACHE_SECONDS = 60.0


def _selectable_runtime_ids(
    catalog,
    *,
    explicit_runtime: str | None,
    profile_pin: str | None,
    global_selection,
    project_selection,
) -> frozenset[str]:
    """The runtimes whose availability can change this selection.

    An explicit or profile choice needs only that runtime. Otherwise only
    runtimes some source could choose are probed: rule targets (project rules
    only when trusted), the classifier's candidates when it may run, and the
    default and fallback runtimes. A plain run with none of these probes
    nothing and stays native.
    """
    known = {m.runtime_id for m in catalog.registry.manifests}

    def resolve(ref: str | None) -> set[str]:
        if not ref:
            return set()
        try:
            return {catalog.registry.get(ref).runtime_id}
        except Exception:
            return {ref} & known

    if explicit_runtime or profile_pin:
        return frozenset(resolve(explicit_runtime or profile_pin))
    wanted: set[str] = set()
    for rule in global_selection.rules:
        wanted |= resolve(rule.runtime)
    if global_selection.trust_project_routes:
        for rule in project_selection.rules:
            wanted |= resolve(rule.runtime)
    classifier = global_selection.classifier
    if classifier.enabled and not project_selection.classifier_disabled:
        if classifier.candidates is None:
            wanted |= known
        else:
            for ref in classifier.candidates:
                wanted |= resolve(ref)
    wanted |= resolve(global_selection.default_runtime)
    wanted |= resolve(global_selection.fallback_runtime)
    return frozenset(wanted)


def _initial_selection_inputs(
    *,
    workspace: str,
    task: str,
    catalog: RuntimeCatalog | None,
    agent: str,
    mode: str,
    explicit_runtime: str | None,
    profile_pin: str | None,
    workspace_kind: str,
    permission_ceiling: str,
    required_capabilities: tuple[str, ...],
    available_runtime_ids: frozenset[str] | None,
) -> tuple[Any, list[Any], Any, Any, Any]:
    """Trusted facts shared by the synchronous and classifier selection paths.

    Returns ``(request, candidates, global_selection, project_selection,
    traits)`` built from one catalog so every path sees the same registry,
    disabled set, and executable availability as launch.
    """
    from garuda.acp.catalog import load_trusted_runtime_settings
    from garuda.config.agent_home import resolve_agent_home
    from garuda.runtime.selection import (
        InitialCandidate,
        InitialRequest,
        detect_repo_traits,
        parse_global_selection,
        parse_project_selection,
    )

    home = resolve_agent_home(workspace)
    global_settings = load_trusted_runtime_settings()
    project_settings = getattr(home, "settings", None) or {}
    catalog = catalog or prepare_runtime_catalog(workspace)
    global_selection = parse_global_selection(global_settings)
    project_selection = parse_project_selection(project_settings)
    wanted = _selectable_runtime_ids(
        catalog,
        explicit_runtime=explicit_runtime,
        profile_pin=profile_pin,
        global_selection=global_selection,
        project_selection=project_selection,
    )
    discovered = {
        entry.runtime_id: entry
        for entry in catalog.discover(only=wanted, cache_ttl=SELECTION_PROBE_CACHE_SECONDS)
    }
    candidates: list[InitialCandidate] = []
    for manifest in catalog.registry.manifests:
        entry = discovered.get(manifest.runtime_id)
        forced = available_runtime_ids is not None and manifest.runtime_id in available_runtime_ids
        available = forced or bool(getattr(entry, "available", True))
        candidates.append(
            InitialCandidate(
                runtime_id=manifest.runtime_id,
                kind=getattr(manifest.kind, "value", str(manifest.kind)),
                available=available,
                health="ok" if forced else getattr(entry, "health", "ok"),
                auth=getattr(entry, "auth", "unknown"),
                capabilities=tuple(manifest.capabilities.names),
                unavailable_reason=(
                    "; ".join(getattr(entry, "warnings", ()) or ())
                    if entry is not None and not available
                    else ""
                ),
            )
        )
    request = InitialRequest(
        task=task,
        agent=agent,
        mode=mode,
        workspace_kind=workspace_kind,
        permission_ceiling=permission_ceiling,
        explicit_runtime=explicit_runtime,
        profile_pin=profile_pin,
        default_runtime=global_selection.default_runtime,
        fallback_runtime=global_selection.fallback_runtime,
        required_capabilities=required_capabilities,
        workspace=workspace,
    )
    return request, candidates, global_selection, project_selection, detect_repo_traits(workspace)


def select_initial_runtime(
    *,
    workspace: str,
    task: str,
    catalog: RuntimeCatalog | None = None,
    agent: str = "",
    mode: str = "",
    explicit_runtime: str | None = None,
    profile_pin: str | None = None,
    workspace_kind: str = "local",
    permission_ceiling: str = "smart",
    required_capabilities: tuple[str, ...] = (),
    available_runtime_ids: frozenset[str] | None = None,
) -> InitialRuntimeSelection:
    """Plan initial selection from the exact catalog the caller will launch.

    Selection stays pure in :mod:`garuda.runtime.selection`; this boundary owns
    trusted configuration and discovery. Passing a catalog is the normal
    product path and prevents selection and launch from observing different
    disablement or executable availability facts. This synchronous path is
    deterministic only; :func:`select_initial_runtime_async` adds the optional
    classifier fallback.
    """
    from garuda.runtime.selection import select_initial

    request, candidates, global_selection, project_selection, traits = _initial_selection_inputs(
        workspace=workspace,
        task=task,
        catalog=catalog,
        agent=agent,
        mode=mode,
        explicit_runtime=explicit_runtime,
        profile_pin=profile_pin,
        workspace_kind=workspace_kind,
        permission_ceiling=permission_ceiling,
        required_capabilities=required_capabilities,
        available_runtime_ids=available_runtime_ids,
    )
    selection = select_initial(
        request,
        candidates,
        rules=global_selection.rules,
        project_rules=project_selection.rules,
        traits=traits,
        trust_project_routes=global_selection.trust_project_routes,
    )
    return InitialRuntimeSelection(selection, request, tuple(candidates))


def resolve_runtime_classifier(
    policy: Any,
    *,
    project_disabled: bool = False,
    classifier_model: Any | None = None,
    global_orchestration: Any | None = None,
    factory: ModelFactory | None = None,
    cost_estimator: Any | None = None,
) -> tuple[Any | None, str | None]:
    """Bind the optional initial-runtime classifier to an approved model role.

    Returns ``(classifier, skip_reason)``. The model comes only from trusted
    global configuration: the policy's binding alias (or the global default
    binding) supplies its ``collection`` model, or its ``reasoning`` model when
    the policy names that role or explicitly allows the reasoning fallback.
    Anything that cannot be bound skips classification with a reason; it
    never raises, because a missing classifier only means the configured
    default runtime is used. The built client is limited to one transport
    attempt. ``classifier_model`` lets an SDK caller supply a ``Model`` for
    the policy's role. ``cost_estimator(model_name, usage)`` is injected by the
    entry point (agent packages stay independent of the eval package); without one
    only a provider-reported cost counts and anything else stays unknown.
    """
    from dataclasses import replace

    from garuda.runtime.selection import RuntimeClassifier

    if policy is None or not getattr(policy, "enabled", False):
        return None, None
    if project_disabled:
        return None, "disabled by project settings"
    if classifier_model is not None:
        name = getattr(classifier_model, "model_name", None)
        if not isinstance(name, str) or not callable(getattr(classifier_model, "complete", None)):
            return None, "supplied classifier model does not satisfy the Model protocol"
        return (
            RuntimeClassifier(
                model=classifier_model,
                binding_role=policy.model_role,
                policy=policy,
                cost_estimator=cost_estimator,
            ),
            None,
        )
    if global_orchestration is None:
        from garuda.config.routing import load_global_orchestration

        try:
            global_orchestration = load_global_orchestration()
        except ConfigError as exc:
            return None, f"model bindings unavailable ({type(exc).__name__})"
    bindings_by_alias = global_orchestration.model_bindings
    alias = policy.model_binding
    if alias is not None:
        binding = bindings_by_alias.get(alias)
        if binding is None:
            return None, f"unknown model binding alias {alias!r}"
    else:
        binding = bindings_by_alias.get(global_orchestration.default_binding)
        if binding is None and len(bindings_by_alias) == 1:
            binding = next(iter(bindings_by_alias.values()))
        if binding is None:
            # Never fall back to the built-in model: trusted configuration
            # must name the binding the classifier spends on.
            return None, "no model binding is configured for the classifier"
    role = policy.model_role
    spec = binding.collection if role == "collection" else binding.reasoning
    if spec is None:
        if not policy.allow_reasoning_fallback:
            return None, "no collection model is bound and the reasoning fallback is not allowed"
        role, spec = "reasoning", binding.reasoning
    timeout = policy.timeout_sec if spec.timeout_sec is None else min(spec.timeout_sec, policy.timeout_sec)
    # One attempt and the policy's output budget: thinking/reasoning knobs
    # would raise the effective output ceiling (or spend it on reasoning), so
    # the classifier call drops them.
    spec = replace(
        spec,
        max_attempts=1,
        timeout_sec=timeout,
        thinking_budget_tokens=None,
        reasoning_effort=None,
    )
    try:
        model = (factory or ModelFactory()).build_spec(spec, role=f"classifier ({role})")
    except Exception as exc:
        return None, f"classifier model could not be built ({type(exc).__name__})"
    return (
        RuntimeClassifier(
            model=model, binding_role=role, policy=policy, cost_estimator=cost_estimator
        ),
        None,
    )


async def select_initial_runtime_async(
    *,
    workspace: str,
    task: str,
    catalog: RuntimeCatalog | None = None,
    agent: str = "",
    mode: str = "",
    explicit_runtime: str | None = None,
    profile_pin: str | None = None,
    workspace_kind: str = "local",
    permission_ceiling: str = "smart",
    required_capabilities: tuple[str, ...] = (),
    available_runtime_ids: frozenset[str] | None = None,
    classifier_model: Any | None = None,
    cost_estimator: Any | None = None,
) -> InitialRuntimeSelection:
    """:func:`select_initial_runtime` plus the optional classifier fallback (#80).

    The classifier is bound only when trusted global ``routing.classifier``
    enables it and the project has not disabled it, and it is called only when
    no explicit, profile, or rule source selected. Its answer is revalidated
    against the same candidate facts launch uses.
    """
    from garuda.runtime.selection import select_initial_async

    request, candidates, global_selection, project_selection, traits = _initial_selection_inputs(
        workspace=workspace,
        task=task,
        catalog=catalog,
        agent=agent,
        mode=mode,
        explicit_runtime=explicit_runtime,
        profile_pin=profile_pin,
        workspace_kind=workspace_kind,
        permission_ceiling=permission_ceiling,
        required_capabilities=required_capabilities,
        available_runtime_ids=available_runtime_ids,
    )
    classifier = None
    skip_reason = None
    if explicit_runtime is None and profile_pin is None:
        # Deterministic explicit/profile sources never need a model client;
        # rules are checked inside the selector before the call is made.
        classifier, skip_reason = resolve_runtime_classifier(
            global_selection.classifier,
            project_disabled=project_selection.classifier_disabled,
            classifier_model=classifier_model,
            cost_estimator=cost_estimator,
        )
    selection = await select_initial_async(
        request,
        candidates,
        rules=global_selection.rules,
        project_rules=project_selection.rules,
        traits=traits,
        trust_project_routes=global_selection.trust_project_routes,
        classifier=classifier,
        classifier_skip_reason=skip_reason,
    )
    return InitialRuntimeSelection(selection, request, tuple(candidates))


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
    from garuda.agents.resolve import check_references

    check_references(profile, workspace, mcp_config_path=mcp_config_path)
    # Before anything starts: a repository's own profile cannot raise its
    # permission mode above the user's project ceiling.
    from garuda.agents.authority import PROJECT, enforce_project_ceiling, profile_authority

    profile_home = resolve_agent_home(workspace)
    enforce_project_ceiling(
        profile_name=profile.name,
        declared_mode=profile.permission_mode,
        source_path=profile.source_path,
        workspace=workspace,
        explicit_permission_mode=permission_mode,
        global_settings=profile_home.global_settings,
    )
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
    config.system_prompt = resolve_system_prompt(
        profile, workspace, diagnostics=config.prompt_diagnostics
    )

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
    # Config files the user chose are theirs; a project profile's own
    # `mcp_config_path` is repository content like the rest of the project.
    mcp_user_paths = [mcp_config_path] if mcp_config_path else []
    if (
        not mcp_config_path
        and config.mcp_config_path
        and profile_authority(profile.source_path, workspace) != PROJECT
    ):
        mcp_user_paths.append(config.mcp_config_path)
    permissions = PermissionEngine(
        mode=config.permission_mode,
        tool_rules=profile.tool_rules,
        path_rules=profile.path_rules,
        bash_rules=profile.bash_rules,
        approval_handler=approval_handler,
    )
    toolkit_names = profile.tools
    if resolved.collection is not None and collection_policy.enabled and toolkit_names is not None:
        # Resolve enough tools for both roles once. prepare_run applies the
        # parent's profile filter before inserting delegate_collection, while
        # CollectionCoordinator independently intersects the collector profile
        # with the trusted non-mutating ceiling.
        collection_profile = load_profile(collection_policy.profile, extra_dir=agents_dirs)
        if collection_profile.tools is not None:
            toolkit_names = list(dict.fromkeys([*toolkit_names, *collection_profile.tools]))
    tools, mcp_manager = await build_toolkit(
        toolkit_names,
        mcp_paths,
        extra_tools=extra_tools,
        workspace=workspace,
        load_project_tools=load_project_tools,
        mcp_servers=profile.mcp_servers,
        mcp_user_paths=mcp_user_paths,
    )
    if profile.tools is not None:
        # A misspelled tool name used to vanish without a trace.
        available = {tool.name for tool in tools}
        missing = [
            name
            for name in dict.fromkeys(profile.tools)
            if name not in available and not str(name).startswith("mcp__")
        ]
        if missing:
            logger.warning(
                "Profile %r (%s): unknown tool(s) ignored: %s",
                profile.name,
                profile.source_path or "built-in",
                ", ".join(map(str, missing)),
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
