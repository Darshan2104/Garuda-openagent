"""Shared agent and trusted runtime setup for every launch entry point."""

from dataclasses import dataclass
from pathlib import Path

from garuda.agents.loader import AgentProfile, load_profile, resolve_system_prompt
from garuda.core.modes import apply_mode_preset
from garuda.core.permissions import PermissionEngine
from garuda.core.rigorous import create_agent
from garuda.mcp.config import resolve_mcp_config_paths
from garuda.tools import build_toolkit
from garuda.tools.protocol import Tool
from garuda.types import AgentConfig


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
