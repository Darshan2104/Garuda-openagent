"""Harness manifest catalog, discovery, and health (P1.1, issue #31).

Built-in stubs plus generic configured manifests resolve to `DiscoveredRuntime`
records: executable presence, probed version, login state, capabilities, and
warnings. Discovery only ever runs the version/auth probes a trusted global
manifest declares — it never synthesizes login, install, or token-reading
commands, and unknown versions or missing tools surface as guidance instead
of guesses.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from garuda.acp.adapter import AcpRuntime
from garuda.acp.authority import AuthorityPolicy
from garuda.runtime.protocol import AuthStatus, HealthStatus, RuntimeKind

BUILTIN_DIR = os.path.join(os.path.dirname(__file__), "builtin")
BUILTIN_MANIFEST_FILES = ("claude.json", "codex.json", "cursor.json", "opencode.json")

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
    probe_timeout: float = PROBE_TIMEOUT,
    run_probe: Callable[..., str | None] | None = None,
) -> list[DiscoveredRuntime]:
    """Inspect configured harnesses without logging in, installing, or reading tokens."""
    run = run_probe or _run_probe
    by_id = {m.runtime_id: m for m in manifests}
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
        found.append(_inspect(manifest, run, probe_timeout))
    for stub in BUILTIN_STUBS:
        if stub["runtime_id"] not in by_id and stub["runtime_id"] not in disabled:
            found.append(
                DiscoveredRuntime(
                    runtime_id=stub["runtime_id"],
                    kind=RuntimeKind.ACP.value,
                    available=False,
                    auth=AuthStatus.UNKNOWN,
                    warnings=(stub["description"] + " " + stub["setup"],),
                )
            )
    return sorted(found, key=lambda d: d.runtime_id)


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
    policy: dict[str, AuthorityPolicy] | None = None,
) -> AcpRuntime:
    """Build the generic adapter for one manifest. Tests override argv with fakes."""
    return AcpRuntime(
        list(argv_override) if argv_override is not None else list(manifest.command or ()),
        runtime_id=manifest.runtime_id,
        policy=policy,
        setup_hint=manifest.setup,
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
    """Resolve a manifest to launch argv, or refuse loudly with setup guidance."""
    if not manifest.command:
        raise AcpUnavailableError(manifest.runtime_id, manifest.setup or "no launch command configured")
    if executable is None:
        raise AcpUnavailableError(manifest.runtime_id, manifest.setup or "executable not found")
    return list(manifest.command)
