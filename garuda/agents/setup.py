"""Shared agent and trusted runtime setup for every launch entry point."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from garuda.agents.loader import AgentProfile, load_profile, resolve_system_prompt
from garuda.core.modes import apply_mode_preset
from garuda.core.permissions import PermissionEngine
from garuda.core.rigorous import create_agent
from garuda.mcp.config import resolve_mcp_config_paths
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


@dataclass(frozen=True)
class RuntimeCatalog:
    """The one trusted registry and inspectable discovery result for a run.

    Global settings authorize executable manifests and disablement. Project
    settings may only name aliases and offer disablement suggestions; neither
    can authorize a command or override the global disabled set.
    """

    registry: object
    discovered: tuple[object, ...]

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


def prepare_runtime_catalog(workspace: str | Path) -> RuntimeCatalog:
    """Build the shared trusted runtime boundary for CLI and SDK launches.

    This deliberately reads the global file through the strict runtime loader,
    rather than ``AgentHome.global_settings``: malformed global YAML must not
    erase a user's safety disablement and silently authorize a launch.
    """
    from garuda.acp.catalog import (
        discover,
        load_trusted_disabled,
        load_trusted_runtime_settings,
    )
    from garuda.config.agent_home import resolve_agent_home
    from garuda.runtime.registry import (
        RuntimeRegistry,
        parse_global_manifests,
        parse_project_refs,
    )

    global_settings = load_trusted_runtime_settings()
    home = resolve_agent_home(workspace)
    manifests = parse_global_manifests(
        global_settings.get("runtimes"), source="trusted global runtimes"
    )
    project_refs = parse_project_refs(
        home.settings.get("runtime_refs"), source=f"project runtime refs ({home.workspace})"
    )
    disabled = load_trusted_disabled(global_settings)
    project_disabled = home.settings.get("disabled_runtimes", [])
    if project_disabled is None:
        project_disabled = []
    if not isinstance(project_disabled, list) or any(
        not isinstance(runtime_id, str) or not runtime_id for runtime_id in project_disabled
    ):
        raise ValueError("project disabled_runtimes must be a list of runtime id strings")
    registry = RuntimeRegistry(manifests, project_refs, disabled=disabled)
    return RuntimeCatalog(
        registry=registry,
        discovered=tuple(
            discover(
                registry.manifests,
                disabled=registry.disabled_ids,
                project_disabled=frozenset(project_disabled),
            )
        ),
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
) -> tuple[AgentProfile, AgentConfig, PermissionEngine, list, object, object | None]:
    """Load profile, resolve skills, build toolkit, and return run dependencies.

    ``permission_mode`` overrides both the profile's declaration and the mode preset.
    It has to be applied here rather than by the caller because ``PermissionEngine`` takes
    its mode at construction and exposes no setter — a caller that assigned
    ``config.permission_mode`` afterwards would change the reported posture while the
    engine kept enforcing the old one, which is the worst of the three outcomes.
    """
    from garuda.config.agent_home import resolve_agents_dirs

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
    config.system_prompt = resolve_system_prompt(profile, workspace)
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
    return profile, config, permissions, tools, agent, mcp_manager
