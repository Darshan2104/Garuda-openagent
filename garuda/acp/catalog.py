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

import os
import re
import shutil
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from garuda.runtime.protocol import AuthStatus, HealthStatus, RuntimeKind

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
            list(argv), capture_output=True, text=True, timeout=timeout, env=_minimal_env()
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
        return binary if os.access(binary, os.X_OK) else None
    return shutil.which(binary)


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
                )
            )
            continue
        record = _inspect(manifest, run, probe_timeout)
        if manifest.runtime_id in project_warned:
            record = DiscoveredRuntime(
                runtime_id=record.runtime_id,
                kind=record.kind,
                available=record.available,
                executable=record.executable,
                version=record.version,
                auth=record.auth,
                health=record.health,
                capabilities=record.capabilities,
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
    A present-but-malformed `disabled_runtimes` value raises `ValueError`:
    silently enabling a runtime the user meant to disable is the wrong
    direction to fail.
    """
    if settings is None:
        settings = load_trusted_runtime_settings()
    if not isinstance(settings, Mapping):
        raise ValueError("global settings must be a mapping")
    raw = settings.get("disabled_runtimes", [])
    if raw is None or raw == []:
        return frozenset()
    if not isinstance(raw, list) or any(not isinstance(v, str) or not v for v in raw):
        raise ValueError("disabled_runtimes must be a list of runtime id strings")
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
        raise ValueError(f"cannot read trusted runtime settings {path}: {exc}") from exc
    if not isinstance(data, Mapping):
        raise ValueError(f"trusted runtime settings {path} must be a mapping")
    return data


def _inspect(manifest, run: Callable[..., str | None], timeout: float) -> DiscoveredRuntime:
    warnings: list[str] = list(manifest.warnings)
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
    )


def health_of(discovered: DiscoveredRuntime) -> dict[str, Any]:
    """JSON-safe health record for CLI/SDK/dashboard consumers."""
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
    }
