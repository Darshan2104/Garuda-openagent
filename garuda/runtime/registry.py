"""Runtime registry and declarative harness configuration (P0.5, issue #12).

Trust model, mirroring `config/agent_home.py`: the user's global configuration
is the trust anchor and the *only* place that authorizes an executable. A
project reference names a registered runtime id and may narrow capabilities —
it can never define a command, widen capabilities, or smuggle secrets.

There is no process-global registry: every `RuntimeRegistry` instance is built
explicitly, so server jobs cannot leak registrations into each other.
"""

from __future__ import annotations

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
    {"runtime_id", "kind", "command", "version", "capabilities", "description", "warnings"}
)


class RegistryError(AgentRuntimeError):
    """Malformed configuration or an unresolvable reference. Fail-closed."""


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
    ):
        manifests = list(manifests or [])
        project_refs = list(project_refs or [])
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

    def get(self, ref: str) -> ResolvedRuntime:
        """Resolve a runtime id or project alias. Unknown refs fail closed."""
        if ref in self._manifests:
            manifest = self._manifests[ref]
            return ResolvedRuntime(
                runtime_id=manifest.runtime_id,
                kind=manifest.kind,
                version=manifest.version,
                command=manifest.command,
                capabilities=manifest.capabilities,
                description=manifest.description,
                warnings=manifest.warnings,
            )
        project_ref = self._aliases.get(ref)
        if project_ref is None:
            raise RegistryError(f"unknown runtime {ref!r}")
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
            warnings=manifest.warnings,
            via_alias=project_ref.alias,
        )

    def list(self) -> list[ResolvedRuntime]:
        """Every registered runtime, including aliases, in deterministic order."""
        ids = sorted(self._manifests)
        aliases = sorted(self._aliases)
        return [self.get(ref) for ref in ids + aliases]

    def default(self) -> ResolvedRuntime:
        return self.get(BUILTIN_NATIVE_ID)
