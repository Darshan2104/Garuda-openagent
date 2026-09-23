"""Runtime registry and declarative harness configuration (P0.5, issue #12).

Trust model, mirroring `config/agent_home.py`: the user's global configuration
is the trust anchor and the *only* place that authorizes an executable. A
project reference names a registered runtime id and may narrow capabilities —
it can never define a command, widen capabilities, or smuggle secrets.

There is no process-global registry: every `RuntimeRegistry` instance is built
explicitly, so server jobs cannot leak registrations into each other.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from garuda.runtime.protocol import (
    AgentRuntimeError,
    RuntimeCapabilities,
    RuntimeKind,
)

#: The in-process native loop. Always present; needs no command.
BUILTIN_NATIVE_ID = "native"

#: Manifest keys that would carry secrets. Rejected by name so configuration
#: holds commands and non-secret metadata only — never environment passthrough,
#: tokens, or credential paths.
_FORBIDDEN_MANIFEST_KEYS = frozenset(
    {"env", "environment", "secrets", "credentials", "token", "api_key", "auth"}
)

_MANIFEST_FIELDS = frozenset(
    {
        "runtime_id",
        "kind",
        "command",
        "version",
        "capabilities",
        "description",
        "warnings",
        "version_args",
        "version_pattern",
        "auth_probe",
        "setup",
        "login",
    }
)


class RegistryError(AgentRuntimeError):
    """Malformed configuration or an unresolvable reference. Fail-closed."""


@dataclass(frozen=True)
class LoginFlow:
    """How a user authenticates, declared — never performed — by Garuda.

    Only user-driven flows exist: `user-cli` (run the vendor login yourself)
    and `api-key` (export the key yourself). Garuda opens no login flow on
    its own, and missing credentials only ever produce guidance.
    """

    flow: str = "user-cli"
    instructions: str = ""

    @classmethod
    def parse(cls, value: object, *, where: str = "login") -> "LoginFlow":
        if value is None:
            return cls()
        if not isinstance(value, dict):
            raise RegistryError(f"{where}: login must be a mapping")
        unknown = set(value) - {"flow", "instructions"}
        if unknown:
            raise RegistryError(f"{where}: unknown login fields {sorted(unknown)}")
        flow = value.get("flow", "user-cli")
        if flow not in ("user-cli", "api-key"):
            raise RegistryError(
                f"{where}.flow: must be 'user-cli' or 'api-key', got {flow!r}"
            )
        instructions = value.get("instructions", "")
        if not isinstance(instructions, str):
            raise RegistryError(f"{where}.instructions: must be a string")
        return cls(flow=flow, instructions=instructions)


@dataclass(frozen=True)
class AuthProbe:
    """How to check login state without logging in: run `argv`, match output.

    A match on `authenticated_pattern` means logged in, on
    `unauthenticated_pattern` means logged out, anything else is UNKNOWN —
    never guessed. The probe must not install, authenticate, or read tokens.
    """

    argv: tuple[str, ...]
    authenticated_pattern: str = ""
    unauthenticated_pattern: str = ""


@dataclass(frozen=True)
class RuntimeManifest:
    """One trusted global harness definition. Authorizes `command` to run."""

    runtime_id: str
    kind: RuntimeKind
    version: str
    command: tuple[str, ...] | None = None
    capabilities: RuntimeCapabilities = field(default_factory=RuntimeCapabilities)
    description: str = ""
    warnings: tuple[str, ...] = ()
    version_args: tuple[str, ...] | None = None
    version_pattern: str = ""
    auth_probe: AuthProbe | None = None
    setup: str = ""
    login: LoginFlow = field(default_factory=LoginFlow)


@dataclass(frozen=True)
class ProjectRuntimeRef:
    """A project's alias for a registered runtime. References only, no commands."""

    alias: str
    runtime_id: str
    capabilities: frozenset[str] | None = None


@dataclass(frozen=True)
class ResolvedRuntime:
    """Inspectable resolved descriptor: id, kind, version, command, capabilities."""

    runtime_id: str
    kind: RuntimeKind
    version: str
    command: tuple[str, ...] | None
    capabilities: RuntimeCapabilities
    description: str = ""
    setup: str = ""
    warnings: tuple[str, ...] = ()
    via_alias: str | None = None


def _require_str(value: object, *, where: str) -> str:
    if not isinstance(value, str) or not value:
        raise RegistryError(f"{where}: must be a non-empty string")
    return value


def _parse_kind(value: object, *, where: str) -> RuntimeKind:
    if value not in ("native", "acp"):
        raise RegistryError(f"{where}: kind must be 'native' or 'acp', got {value!r}")
    return RuntimeKind(value)


def _parse_command(value: object, *, where: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise RegistryError(f"{where}: command must be a non-empty argv list")
    for part in value:
        if not isinstance(part, str) or not part:
            raise RegistryError(f"{where}: command entries must be non-empty strings")
    return tuple(value)


def _parse_capabilities(value: object, *, where: str) -> RuntimeCapabilities:
    if value is None:
        return RuntimeCapabilities()
    if not isinstance(value, list):
        raise RegistryError(f"{where}: capabilities must be a list of names")
    for name in value:
        if not isinstance(name, str) or not name:
            raise RegistryError(f"{where}: capability names must be non-empty strings")
    return RuntimeCapabilities(names=frozenset(value))


def _parse_auth_probe(value: object, *, where: str) -> AuthProbe | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise RegistryError(f"{where}: auth_probe must be a mapping")
    unknown = set(value) - {"argv", "authenticated_pattern", "unauthenticated_pattern"}
    if unknown:
        raise RegistryError(f"{where}: unknown auth_probe fields {sorted(unknown)}")
    argv = _parse_command(value.get("argv"), where=f"{where}.argv")
    for key in ("authenticated_pattern", "unauthenticated_pattern"):
        pattern = value.get(key, "")
        if not isinstance(pattern, str):
            raise RegistryError(f"{where}.{key}: must be a string")
        try:
            re.compile(pattern)
        except re.error as exc:
            raise RegistryError(f"{where}.{key}: invalid regex: {exc}") from exc
    return AuthProbe(
        argv=argv,
        authenticated_pattern=value.get("authenticated_pattern", "") or "",
        unauthenticated_pattern=value.get("unauthenticated_pattern", "") or "",
    )


def parse_global_manifests(data: object, *, source: str = "global runtimes") -> list[RuntimeManifest]:
    """Parse trusted global manifests. Unknown or secret-carrying keys fail closed."""
    if data is None:
        return []
    if not isinstance(data, list):
        raise RegistryError(f"{source}: must be a list of manifests")
    manifests = []
    for index, item in enumerate(data):
        where = f"{source}[{index}]"
        if not isinstance(item, dict):
            raise RegistryError(f"{where}: must be a mapping")
        forbidden = set(item) & _FORBIDDEN_MANIFEST_KEYS
        if forbidden:
            raise RegistryError(
                f"{where}: secret-carrying keys are forbidden: {sorted(forbidden)}"
            )
        unknown = set(item) - _MANIFEST_FIELDS
        if unknown:
            raise RegistryError(f"{where}: unknown fields {sorted(unknown)}")
        runtime_id = _require_str(item.get("runtime_id"), where=f"{where}.runtime_id")
        kind = _parse_kind(item.get("kind"), where=f"{where}.kind")
        version = _require_str(item.get("version", "unknown"), where=f"{where}.version")
        command = None
        if kind is RuntimeKind.ACP:
            if "command" not in item:
                raise RegistryError(f"{where}: acp runtimes require a command")
            command = _parse_command(item["command"], where=f"{where}.command")
        elif "command" in item:
            raise RegistryError(f"{where}: native runtimes run in-process, no command allowed")
        warnings = item.get("warnings", [])
        if not isinstance(warnings, list) or any(
            not isinstance(w, str) for w in warnings
        ):
            raise RegistryError(f"{where}.warnings: must be a list of strings")
        description = item.get("description", "")
        if not isinstance(description, str):
            raise RegistryError(f"{where}.description: must be a string")
        version_args = None
        if item.get("version_args") is not None:
            version_args = _parse_command(item["version_args"], where=f"{where}.version_args")
        version_pattern = item.get("version_pattern", "")
        if not isinstance(version_pattern, str):
            raise RegistryError(f"{where}.version_pattern: must be a string")
        if version_pattern:
            try:
                re.compile(version_pattern)
            except re.error as exc:
                raise RegistryError(f"{where}.version_pattern: invalid regex: {exc}") from exc
        setup = item.get("setup", "")
        if not isinstance(setup, str):
            raise RegistryError(f"{where}.setup: must be a string")
        manifests.append(
            RuntimeManifest(
                runtime_id=runtime_id,
                kind=kind,
                version=version,
                command=command,
                capabilities=_parse_capabilities(
                    item.get("capabilities"), where=f"{where}.capabilities"
                ),
                description=description,
                warnings=tuple(warnings),
                version_args=version_args,
                version_pattern=version_pattern,
                auth_probe=_parse_auth_probe(item.get("auth_probe"), where=f"{where}.auth_probe"),
                setup=setup,
                login=LoginFlow.parse(item.get("login"), where=f"{where}.login"),
            )
        )
    return manifests


def parse_project_refs(data: object, *, source: str = "project runtimes") -> list[ProjectRuntimeRef]:
    """Parse project aliases. Any `command` here is a self-authorization attempt."""
    if data is None:
        return []
    if not isinstance(data, list):
        raise RegistryError(f"{source}: must be a list of references")
    refs = []
    for index, item in enumerate(data):
        where = f"{source}[{index}]"
        if not isinstance(item, dict):
            raise RegistryError(f"{where}: must be a mapping")
        if "command" in item:
            raise RegistryError(
                f"{where}: project configuration cannot authorize an executable"
            )
        unknown = set(item) - {"alias", "runtime_id", "capabilities"}
        if unknown:
            raise RegistryError(f"{where}: unknown fields {sorted(unknown)}")
        runtime_id = _require_str(item.get("runtime_id"), where=f"{where}.runtime_id")
        alias = item.get("alias") or runtime_id
        if not isinstance(alias, str) or not alias:
            raise RegistryError(f"{where}.alias: must be a non-empty string")
        capabilities = None
        if item.get("capabilities") is not None:
            capabilities = _parse_capabilities(
                item["capabilities"], where=f"{where}.capabilities"
            ).names
        refs.append(ProjectRuntimeRef(alias=alias, runtime_id=runtime_id, capabilities=capabilities))
    return refs


class RuntimeRegistry:
    """Resolved harness configuration. Build one per job; share nothing globally."""

    def __init__(
        self,
        manifests: list[RuntimeManifest] | None = None,
        project_refs: list[ProjectRuntimeRef] | None = None,
        disabled: frozenset[str] | set[str] | None = None,
    ):
        """`disabled` is the trusted global set (see `catalog.load_trusted_disabled`).

        The builtin native runtime cannot be disabled — it is the fallback every
        selection path assumes. Unknown ids in the set are inert (a stale entry
        must not brick the registry); malformed entries fail closed.
        """
        manifests = list(manifests or [])
        project_refs = list(project_refs or [])
        disabled_set = frozenset(disabled or ())
        for entry in disabled_set:
            if not isinstance(entry, str) or not entry:
                raise RegistryError(f"disabled runtime ids must be non-empty strings, got {entry!r}")
        if BUILTIN_NATIVE_ID in disabled_set:
            raise RegistryError("the builtin native runtime cannot be disabled")
        self._disabled = disabled_set
        self._manifests: dict[str, RuntimeManifest] = {}
        for manifest in manifests:
            if manifest.runtime_id in self._manifests:
                raise RegistryError(f"duplicate runtime_id: {manifest.runtime_id!r}")
            self._manifests[manifest.runtime_id] = manifest
        if BUILTIN_NATIVE_ID not in self._manifests:
            self._manifests[BUILTIN_NATIVE_ID] = RuntimeManifest(
                runtime_id=BUILTIN_NATIVE_ID,
                kind=RuntimeKind.NATIVE,
                version="builtin",
                description="The in-process Garuda loop.",
            )
        self._aliases: dict[str, ProjectRuntimeRef] = {}
        for ref in project_refs:
            if ref.alias in self._aliases or ref.alias in self._manifests:
                raise RegistryError(f"duplicate runtime alias: {ref.alias!r}")
            manifest = self._manifests.get(ref.runtime_id)
            if manifest is None:
                raise RegistryError(
                    f"project alias {ref.alias!r} references unknown runtime {ref.runtime_id!r}"
                )
            if ref.capabilities is not None and not ref.capabilities <= manifest.capabilities.names:
                raise RegistryError(
                    f"project alias {ref.alias!r} widens capabilities "
                    f"beyond {ref.runtime_id!r}: "
                    f"{sorted(ref.capabilities - manifest.capabilities.names)}"
                )
            self._aliases[ref.alias] = ref

    @property
    def manifests(self) -> list[RuntimeManifest]:
        """The trusted manifests, builtin native first, for discovery."""
        return [self._manifests[key] for key in sorted(self._manifests)]

    @property
    def disabled_ids(self) -> frozenset[str]:
        """Runtime ids disabled by trusted global configuration."""
        return self._disabled

    def is_disabled(self, ref: str) -> bool:
        """True when resolving `ref` would hit a disabled runtime. Unknown refs
        fail closed like `get`."""
        return self._resolve_id(ref) in self._disabled

    def _resolve_id(self, ref: str) -> str:
        if ref in self._manifests:
            return ref
        project_ref = self._aliases.get(ref)
        if project_ref is None:
            raise RegistryError(f"unknown runtime {ref!r}")
        return project_ref.runtime_id

    def manifest_for(self, ref: str):
        """The parsed manifest behind an id or alias. Same disabled
        enforcement as `get`: unresolvable or disabled refs never reach a
        launcher."""
        runtime_id = self._resolve_id(ref)
        if runtime_id in self._disabled:
            raise RegistryError(
                f"runtime {runtime_id!r} is disabled by user configuration"
            )
        return self._manifests[runtime_id]

    def get(self, ref: str) -> ResolvedRuntime:
        """Resolve a runtime id or project alias.

        Unknown refs fail closed; disabled runtimes — directly or via alias —
        are refused so they cannot be selected or started.
        """
        runtime_id = self._resolve_id(ref)
        if runtime_id in self._disabled:
            raise RegistryError(
                f"runtime {runtime_id!r} is disabled by user configuration"
            )
        if ref in self._manifests:
            manifest = self._manifests[ref]
            return ResolvedRuntime(
                runtime_id=manifest.runtime_id,
                kind=manifest.kind,
                version=manifest.version,
                command=manifest.command,
                capabilities=manifest.capabilities,
                description=manifest.description,
                setup=manifest.setup,
                warnings=manifest.warnings,
            )
        project_ref = self._aliases[ref]
        manifest = self._manifests[project_ref.runtime_id]
        capabilities = manifest.capabilities
        if project_ref.capabilities is not None:
            capabilities = RuntimeCapabilities(names=project_ref.capabilities)
        return ResolvedRuntime(
            runtime_id=manifest.runtime_id,
            kind=manifest.kind,
            version=manifest.version,
            command=manifest.command,
            capabilities=capabilities,
            description=manifest.description,
            setup=manifest.setup,
            warnings=manifest.warnings,
            via_alias=project_ref.alias,
        )

    def list(self) -> list[ResolvedRuntime]:
        """Every registered runtime, including aliases, in deterministic order.

        Disabled entries are listed — UIs need to show them — annotated with
        the disablement warning instead of resolving.
        """
        resolved: list[ResolvedRuntime] = []
        for ref in sorted(self._manifests):
            manifest = self._manifests[ref]
            warnings = manifest.warnings
            if ref in self._disabled:
                warnings = (*warnings, "disabled by user configuration")
            resolved.append(
                ResolvedRuntime(
                    runtime_id=manifest.runtime_id,
                    kind=manifest.kind,
                    version=manifest.version,
                    command=manifest.command,
                    capabilities=manifest.capabilities,
                    description=manifest.description,
                    setup=manifest.setup,
                    warnings=warnings,
                )
            )
        for alias in sorted(self._aliases):
            project_ref = self._aliases[alias]
            try:
                entry = self.get(alias)
            except RegistryError:
                manifest = self._manifests[project_ref.runtime_id]
                capabilities = manifest.capabilities
                if project_ref.capabilities is not None:
                    capabilities = RuntimeCapabilities(names=project_ref.capabilities)
                entry = ResolvedRuntime(
                    runtime_id=manifest.runtime_id,
                    kind=manifest.kind,
                    version=manifest.version,
                    command=manifest.command,
                    capabilities=capabilities,
                    description=manifest.description,
                    setup=manifest.setup,
                    warnings=(*manifest.warnings, "disabled by user configuration"),
                    via_alias=alias,
                )
            resolved.append(entry)
        return resolved

    def default(self) -> ResolvedRuntime:
        return self.get(BUILTIN_NATIVE_ID)
