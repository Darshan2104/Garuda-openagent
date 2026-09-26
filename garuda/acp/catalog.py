"""Harness manifest catalog, discovery, and health (P1.1, issue #31).

Built-in stubs plus generic configured manifests resolve to `DiscoveredRuntime`
records: executable presence, probed version, login state, capabilities, and
warnings. Discovery only ever runs the version/auth probes a trusted global
manifest declares — it never synthesizes login, install, or token-reading
commands, and unknown versions or missing tools surface as guidance instead
of guesses.

Disablement is trusted-global only: `load_trusted_disabled` reads the
`disabled_runtimes` list from the user-level global settings (the same trust
anchor as project-code authorization). Project configuration is
recommendation-only — `project_disabled` entries are reported as warnings on
the discovered record and never applied.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from garuda.acp.adapter import AcpRuntime
from garuda.acp.authority import AuthorityPolicy
from garuda.runtime.protocol import AuthStatus, HealthStatus, RuntimeKind
from garuda.runtime.registry import RuntimeRegistry

logger = logging.getLogger(__name__)

BUILTIN_DIR = os.path.join(os.path.dirname(__file__), "builtin")
BUILTIN_MANIFEST_FILES = (
    "claude.json", "codex.json", "cursor.json", "opencode.json", "pi.json", "goose.json"
)

#: Tested vendor command pairs. Other trusted ACP manifests use the generic
#: path, which supplies protocol and launch protections but no vendor promise.
BUILTIN_COMMANDS = {
    "claude": ("claude-agent-acp",),
    "codex": ("codex-acp",),
    "cursor": ("agent", "acp"),
    "opencode": ("opencode", "acp"),
    "pi": ("pi-acp",),
    "goose": ("goose", "acp"),
}
GENERIC_WARNING = (
    "generic adapter: standard capability set only; "
    "vendor-specific behavior is not guaranteed"
)

#: Harnesses with no configured manifest yet. Each resolves to an unavailable
#: entry explaining exactly how to enable it; tested launch commands arrive
#: with the P1 adapter issues rather than as guesses here.
BUILTIN_STUBS: tuple[dict[str, str], ...] = (
    {
        "runtime_id": "claude",
        "description": "Claude Code (subscription-backed, user-authenticated).",
        "setup": "Add a global harness manifest with its ACP launch command.",
    },
    {
        "runtime_id": "codex",
        "description": "Codex (subscription-backed, user-authenticated).",
        "setup": "Add a global harness manifest with its ACP launch command.",
    },
    {
        "runtime_id": "cursor",
        "description": "Cursor Agent (subscription-backed, user-authenticated).",
        "setup": "Add a global harness manifest with its ACP launch command.",
    },
    {
        "runtime_id": "opencode",
        "description": "OpenCode (subscription-backed, user-authenticated).",
        "setup": "Add a global harness manifest with its ACP launch command.",
    },
    {
        "runtime_id": "pi",
        "description": "Pi agent (subscription-backed, user-authenticated).",
        "setup": "Add a global harness manifest with its ACP launch command.",
    },
    {
        "runtime_id": "goose",
        "description": "Goose (subscription-backed, user-authenticated).",
        "setup": "Add a global harness manifest with its ACP launch command.",
    },
)

PROBE_TIMEOUT = 10.0


class RuntimeSettingsError(ValueError):
    """The trusted runtime settings are unreadable or malformed. Fail-closed."""


@dataclass(frozen=True)
class DiscoveredRuntime:
    runtime_id: str
    kind: str
    available: bool
    executable: str | None = None
    version: str = "unknown"
    auth: AuthStatus = AuthStatus.UNKNOWN
    health: HealthStatus = HealthStatus.UNAVAILABLE
    capabilities: tuple[str, ...] = ()
    warnings: tuple[str, ...] = field(default_factory=tuple)
    login_flow: str = "user-cli"
    login_instructions: str = ""

    def describe(self) -> list[str]:
        lines = [
            f"{self.runtime_id} ({self.kind}): "
            + ("available" if self.available else "unavailable")
        ]
        lines.append(f"  version: {self.version}")
        lines.append(f"  auth: {self.auth.value}")
        if self.executable:
            lines.append(f"  executable: {self.executable}")
        if self.capabilities:
            lines.append(f"  capabilities: {', '.join(self.capabilities)}")
        for warning in self.warnings:
            lines.append(f"  warning: {warning}")
        return lines

    def describe_auth(self) -> list[str]:
        """Login, availability, and quota UX. Missing credentials produce
        guidance plus the vendor flow — never a scan, copy, or sign-in."""
        if self.auth is AuthStatus.AUTHENTICATED:
            lines = [f"{self.runtime_id}: logged in"]
        elif self.auth is AuthStatus.UNAUTHENTICATED:
            lines = [f"{self.runtime_id}: logged out"]
        else:
            lines = [f"{self.runtime_id}: login state unknown (checked only at run time)"]
        if self.auth is not AuthStatus.AUTHENTICATED and self.login_instructions:
            lines.append(f"  to log in: {self.login_instructions}")
        lines.append("  quota: shown only when the harness reports it")
        lines.append("  subscription use is governed by the vendor's policy")
        return lines


def _minimal_env() -> dict[str, str]:
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", ""),
        "LANG": "C.UTF-8",
    }
    return {k: v for k, v in env.items() if v}


def _run_probe(argv: tuple[str, ...], *, timeout: float) -> str | None:
    try:
        result = subprocess.run(
            list(argv),
            capture_output=True,
            text=True,
            timeout=timeout,
            env=_minimal_env(),
            # A probe must never read the user's terminal: an interactive
            # prompt would otherwise block discovery until the timeout.
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout + result.stderr


def _resolve_executable(command: tuple[str, ...] | None) -> str | None:
    if not command:
        return None
    binary = command[0]
    if os.path.isabs(binary):
        return binary if _is_executable_file(binary) else None
    return shutil.which(binary)


def _is_executable_file(path: str) -> bool:
    """A regular file with the execute bit; `os.access` alone accepts directories."""
    return os.path.isfile(path) and os.access(path, os.X_OK)


def discover(
    manifests,
    *,
    disabled: frozenset[str] | set[str] = frozenset(),
    project_disabled: frozenset[str] | set[str] = frozenset(),
    probe_timeout: float = PROBE_TIMEOUT,
    run_probe: Callable[..., str | None] | None = None,
) -> list[DiscoveredRuntime]:
    """Inspect configured harnesses without logging in, installing, or reading tokens.

    `disabled` is the trusted global set: those ids resolve to unavailable
    records and nothing else in the product may select them.
    `project_disabled` is recommendation-only: matching ids stay fully
    available and selectable, carrying a warning that the project suggestion
    was ignored — only global settings disable runtimes.
    """
    run = run_probe or _run_probe
    by_id = {m.runtime_id: m for m in manifests}
    project_warned = set(project_disabled) - set(disabled)
    found: list[DiscoveredRuntime] = []
    for manifest in manifests:
        if manifest.runtime_id in disabled:
            found.append(
                DiscoveredRuntime(
                    runtime_id=manifest.runtime_id,
                    kind=manifest.kind.value,
                    available=False,
                    auth=AuthStatus.UNKNOWN,
                    warnings=("disabled by user configuration",),
                    login_flow=manifest.login.flow,
                    login_instructions=manifest.login.instructions,
                )
            )
            continue
        record = _inspect(manifest, run, probe_timeout)
        if manifest.runtime_id in project_warned:
            # `replace` keeps every other field (login flow, instructions, …)
            # so the advisory warning cannot silently drop login guidance.
            record = replace(
                record,
                warnings=(
                    *record.warnings,
                    "project suggests disabling this runtime — ignored: "
                    "only global settings disable runtimes",
                ),
            )
        found.append(record)
    for stub in BUILTIN_STUBS:
        if stub["runtime_id"] not in by_id:
            warnings = [stub["description"] + " " + stub["setup"]]
            if stub["runtime_id"] in disabled:
                warnings.append("disabled by user configuration")
            if stub["runtime_id"] in project_warned:
                warnings.append(
                    "project suggests disabling this runtime — ignored: "
                    "only global settings disable runtimes"
                )
            found.append(
                DiscoveredRuntime(
                    runtime_id=stub["runtime_id"],
                    kind=RuntimeKind.ACP.value,
                    available=False,
                    auth=AuthStatus.UNKNOWN,
                    warnings=tuple(warnings),
                )
            )
    return sorted(found, key=lambda d: d.runtime_id)


def load_trusted_disabled(settings: Mapping[str, Any] | None = None) -> frozenset[str]:
    """The runtime ids the user disabled in trusted global settings.

    With `settings=None` the global settings file is read. A missing file
    means nothing is disabled, but an unreadable or malformed file is an
    actionable error: substituting an empty mapping could start a runtime the
    user deliberately disabled.
    A present-but-malformed `disabled_runtimes` value raises
    `RuntimeSettingsError` (a `ValueError`):
    silently enabling a runtime the user meant to disable is the wrong
    direction to fail.
    """
    if settings is None:
        settings = load_trusted_runtime_settings()
    if not isinstance(settings, Mapping):
        raise RuntimeSettingsError("global settings must be a mapping")
    raw = settings.get("disabled_runtimes", [])
    if raw is None or raw == []:
        return frozenset()
    if not isinstance(raw, list) or any(not isinstance(v, str) or not v for v in raw):
        raise RuntimeSettingsError("disabled_runtimes must be a list of runtime id strings")
    return frozenset(raw)


def load_trusted_runtime_settings() -> Mapping[str, Any]:
    """Read the runtime trust anchor without converting parse failure to allow.

    Other settings consumers may safely fall back to their built-in defaults.
    Runtime disablement is different: an unreadable trust anchor is ambiguous
    about whether launching an executable is authorized, so callers must stop
    before discovery or selection.
    """
    from garuda.config.agent_home import global_settings_path

    path = global_settings_path()
    if not Path(path).is_file():
        return {}
    try:
        import yaml

        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise RuntimeSettingsError(f"cannot read trusted runtime settings {path}: {exc}") from exc
    if not isinstance(data, Mapping):
        raise RuntimeSettingsError(f"trusted runtime settings {path} must be a mapping")
    return data


def _inspect(manifest, run: Callable[..., str | None], timeout: float) -> DiscoveredRuntime:
    warnings: list[str] = list(manifest.warnings)
    if (
        manifest.kind is not RuntimeKind.NATIVE
        and tuple(manifest.command or ()) != BUILTIN_COMMANDS.get(manifest.runtime_id, ())
    ):
        warnings.append(GENERIC_WARNING)
    if manifest.kind is RuntimeKind.NATIVE:
        return DiscoveredRuntime(
            runtime_id=manifest.runtime_id,
            kind=manifest.kind.value,
            available=True,
            executable=None,
            version=manifest.version,
            auth=AuthStatus.AUTHENTICATED,
            health=HealthStatus.OK,
            capabilities=tuple(sorted(manifest.capabilities.names)),
            warnings=tuple(warnings),
            login_flow=manifest.login.flow,
            login_instructions=manifest.login.instructions,
        )
    executable = _resolve_executable(manifest.command)
    if executable is None:
        binary = manifest.command[0] if manifest.command else "unknown"
        setup = manifest.setup or f"Install the {manifest.runtime_id} CLI and ensure it is on PATH."
        warnings.append(f"executable not found: {binary}. {setup}")
        return DiscoveredRuntime(
            runtime_id=manifest.runtime_id,
            kind=manifest.kind.value,
            available=False,
            auth=AuthStatus.UNKNOWN,
            capabilities=tuple(sorted(manifest.capabilities.names)),
            warnings=tuple(warnings),
            login_flow=manifest.login.flow,
            login_instructions=manifest.login.instructions,
        )
    version = manifest.version
    if manifest.version_args:
        output = run(tuple(manifest.version_args), timeout=timeout)
        if output is not None and manifest.version_pattern:
            match = re.search(manifest.version_pattern, output)
            version = match.group(1) if match and match.groups() else (
                match.group(0) if match else "unknown"
            )
        elif output is not None:
            version = manifest.version
        else:
            version = "unknown"
    auth = AuthStatus.UNKNOWN
    if manifest.auth_probe is not None:
        output = run(tuple(manifest.auth_probe.argv), timeout=timeout)
        if output is not None:
            if manifest.auth_probe.authenticated_pattern and re.search(
                manifest.auth_probe.authenticated_pattern, output
            ):
                auth = AuthStatus.AUTHENTICATED
            elif manifest.auth_probe.unauthenticated_pattern and re.search(
                manifest.auth_probe.unauthenticated_pattern, output
            ):
                auth = AuthStatus.UNAUTHENTICATED
    return DiscoveredRuntime(
        runtime_id=manifest.runtime_id,
        kind=manifest.kind.value,
        available=True,
        executable=executable,
        version=version,
        auth=auth,
        health=HealthStatus.OK,
        capabilities=tuple(sorted(manifest.capabilities.names)),
        warnings=tuple(warnings),
        login_flow=manifest.login.flow,
        login_instructions=manifest.login.instructions,
    )


def health_of(discovered: DiscoveredRuntime, quota: dict[str, Any] | None = None) -> dict[str, Any]:
    """JSON-safe health record for CLI/SDK/dashboard consumers.

    Quota appears only when the harness supplied it (`None` renders unknown,
    never estimated); login opens only the manifest's user-driven flow.
    """
    return {
        "runtime_id": discovered.runtime_id,
        "kind": discovered.kind,
        "available": discovered.available,
        "executable": discovered.executable,
        "version": discovered.version,
        "auth": discovered.auth.value,
        "health": discovered.health.value,
        "capabilities": list(discovered.capabilities),
        "warnings": list(discovered.warnings),
        "login": {
            "flow": discovered.login_flow,
            "instructions": discovered.login_instructions,
        },
        "quota": dict(quota) if quota is not None else None,
    }


def builtin_manifest_dicts() -> list[dict[str, Any]]:
    """Raw JSON dicts of the shipped vendor manifests, for `parse_global_manifests`."""
    dicts = []
    for filename in BUILTIN_MANIFEST_FILES:
        with open(os.path.join(BUILTIN_DIR, filename), encoding="utf-8") as handle:
            data = json.load(handle)
        if not isinstance(data, dict):
            raise ValueError(f"builtin manifest {filename} must be an object")
        dicts.append(data)
    return dicts


def adapter_for_manifest(
    manifest,
    *,
    argv_override: list[str] | None = None,
    executable: str | None = None,
    policy: dict[str, AuthorityPolicy] | None = None,
    cwd: str | None = None,
    store=None,
    approval_handler=None,
    persist_dir: str | None = None,
    metrics=None,
) -> AcpRuntime:
    """Build the generic adapter using the exact executable discovery accepted.

    `executable` must be the absolute path a discovery record resolved (see
    `adapter_for_discovered`); it replaces the manifest's bare command, and no
    second PATH lookup happens here, so a PATH change after discovery cannot
    substitute a different binary. Without it the factory refuses rather than
    re-resolving. ``argv_override`` is only the deterministic test seam for the
    protocol fixture. `cwd` is the absolute session root sent in `session/new`;
    launch paths pass the workspace so the harness never defaults to Garuda's
    own directory. `store` binds the child and active segment to the session
    at start; `approval_handler` answers `session/request_permission` (launch
    paths pass a broker-backed one so asks are parked and audited).
    """
    argv = list(argv_override) if argv_override is not None else require_acp_argv(
        manifest, executable=executable
    )
    return AcpRuntime(
        argv,
        runtime_id=manifest.runtime_id,
        policy=policy,
        cwd=cwd,
        setup_hint=manifest.setup,
        store=store,
        approval_handler=approval_handler,
        persist_dir=persist_dir,
        metrics=metrics,
    )


class AcpUnavailableError(Exception):
    """The ACP path cannot launch. Carries setup guidance so no fallback is silent."""

    def __init__(self, runtime_id: str, setup: str):
        super().__init__(
            f"ACP adapter {runtime_id!r} is unavailable and no non-ACP fallback "
            f"was requested: {setup}"
        )
        self.runtime_id = runtime_id
        self.setup = setup


def require_acp_argv(manifest, *, executable: str | None) -> list[str]:
    """Bind an accepted absolute executable to the manifest's launch argv.

    This is the shared production start gate: the command used for launch is
    the exact path discovery resolved, never a second bare PATH lookup.
    """
    if not manifest.command:
        raise AcpUnavailableError(manifest.runtime_id, manifest.setup or "no launch command configured")
    if not isinstance(executable, str) or not executable:
        raise AcpUnavailableError(manifest.runtime_id, manifest.setup or "executable not found")
    if not os.path.isabs(executable) or not _is_executable_file(executable):
        raise AcpUnavailableError(
            manifest.runtime_id,
            manifest.setup or "discovery did not resolve an executable file",
        )
    return [executable, *manifest.command[1:]]


def adapter_for_discovered(
    manifest,
    discovered: DiscoveredRuntime,
    *,
    policy: dict[str, AuthorityPolicy] | None = None,
    cwd: str | None = None,
    store=None,
    approval_handler=None,
    persist_dir: str | None = None,
) -> AcpRuntime:
    """Launch exactly what a discovery record accepted — the production factory.

    The record must describe this manifest and be available; its resolved
    executable is bound into the argv, so what `discover()` reported is what
    starts.
    """
    if discovered.runtime_id != manifest.runtime_id:
        raise AcpUnavailableError(
            manifest.runtime_id,
            f"discovery record is for {discovered.runtime_id!r}, not this runtime",
        )
    if not discovered.available or not discovered.executable:
        detail = "; ".join(discovered.warnings) or manifest.setup or "executable not found"
        raise AcpUnavailableError(manifest.runtime_id, detail)
    return adapter_for_manifest(
        manifest,
        executable=discovered.executable,
        policy=policy,
        cwd=cwd,
        store=store,
        approval_handler=approval_handler,
        persist_dir=persist_dir,
    )


def shared_registry(
    *,
    extra_manifests: list[dict[str, Any]] | None = None,
    project_refs: list[dict[str, Any]] | None = None,
    disabled: frozenset[str] | set[str] | None = None,
    include_builtins: bool = True,
) -> RuntimeRegistry:
    """Registry for explicit manifest/ref lists, via the one shared builder.

    A thin adapter over `garuda.agents.setup.build_runtime_catalog`, so
    shipped manifests, global overrides, advisory project refs, and
    disablement behave exactly as on the launch path. `disabled=None` reads
    the trusted global set.
    """
    from garuda.agents.setup import build_runtime_catalog

    if disabled is None:
        disabled = load_trusted_disabled()
    return build_runtime_catalog(
        global_settings={"runtimes": list(extra_manifests or [])},
        project_settings={"runtime_refs": list(project_refs or [])},
        disabled=disabled,
        include_builtins=include_builtins,
        source="shared registry project refs",
    ).registry


def discover_for_launch(registry: RuntimeRegistry, ref: str):
    """Resolve `ref` and discover exactly that manifest for a launch.

    Returns `(manifest, record)`. Resolution applies the disabled/alias gates;
    discovery runs only this runtime's declared probes, because a launch is
    about to execute it. An unavailable record refuses here with setup
    guidance, so callers can check before anything moves and later launch the
    very path this record accepted (`adapter_for_discovered`) — never a second
    PATH lookup.
    """
    resolved = registry.get(ref)
    manifest = next(m for m in registry.manifests if m.runtime_id == resolved.runtime_id)
    (record,) = [
        entry
        for entry in discover([manifest], disabled=registry.disabled_ids)
        if entry.runtime_id == manifest.runtime_id
    ]
    if not record.available or not record.executable:
        detail = "; ".join(record.warnings) or manifest.setup or "executable not found"
        raise AcpUnavailableError(manifest.runtime_id, detail)
    # Re-check the accepted path is still an executable file before anyone
    # relies on it; `require_acp_argv` is the shared start gate.
    require_acp_argv(manifest, executable=record.executable)
    return manifest, record


def adapter_for_registry(
    registry: RuntimeRegistry,
    ref: str,
    *,
    argv_override: list[str] | None = None,
    policy: dict[str, AuthorityPolicy] | None = None,
    cwd: str | None = None,
    store=None,
    approval_handler=None,
    persist_dir: str | None = None,
    metrics=None,
) -> AcpRuntime:
    """Resolve through the registry, discover that one manifest, then bind
    the executable discovery accepted (`discover_for_launch` +
    `adapter_for_discovered`).
    """
    if argv_override is not None:
        resolved = registry.get(ref)
        return adapter_for_manifest(
            resolved,
            argv_override=argv_override,
            policy=policy,
            cwd=cwd,
            store=store,
            approval_handler=approval_handler,
            persist_dir=persist_dir,
        )
    manifest, record = discover_for_launch(registry, ref)
    return adapter_for_discovered(
        manifest,
        record,
        policy=policy,
        cwd=cwd,
        store=store,
        approval_handler=approval_handler,
        persist_dir=persist_dir,
        metrics=metrics,
    )
