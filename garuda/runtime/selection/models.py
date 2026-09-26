"""Shared selection types and constants (P1, issue #77).

Dataclasses and bounds for deterministic initial runtime selection. Kept
free of filesystem, parsing, validation, persistence, and orchestration
logic so the other selection modules each own one responsibility.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

#: Version of the persisted selection record. Bump when ``to_dict`` changes.
SELECTION_SCHEMA_VERSION = 1

#: Built-in final fallback. Always present; needs no command.
BUILTIN_NATIVE_ID = "native"

#: Selection sources recorded on the decision, in precedence order.
SOURCE_EXPLICIT = "explicit"
SOURCE_PROFILE = "profile"
SOURCE_RULE = "rule"
SOURCE_CLASSIFIER = "classifier"
SOURCE_DEFAULT = "default"
SOURCE_NATIVE = "native"
SOURCE_FALLBACK = "fallback"

#: Bounds. Routing must stay cheap and non-blocking: a pathological pattern
#: or a huge checkout must degrade to a truncated, explainable result.
MAX_TASK_CHARS = 8_000
MAX_PATTERN_CHARS = 500
MAX_PATTERNS_PER_RULE = 8
MAX_RULES = 64
MAX_TRAIT_FILES = 2_000
MAX_TRAIT_ENTRIES = 5_000
MAX_TRAIT_DEPTH = 6
MAX_MARKER_CHECKS = 64

#: Permission ceilings, strictest first. Mirrors the dashboard ceiling rank so
#: a rule author and the run policy mean the same thing by ``smart``.
PERMISSION_RANK = {"readonly": 0, "smart": 1, "auto": 2, "yolo": 3}

_HEALTH_VALUES = frozenset({"ok", "degraded", "unavailable"})
_AUTH_VALUES = frozenset({"authenticated", "unauthenticated", "expired", "unknown"})


class SelectionError(ValueError):
    """No runtime satisfies the request, or configuration is unusable."""


def _norm(value: object) -> str:
    enum_value = getattr(value, "value", value)
    return str(enum_value or "")


def _norm_set(values: object, *, where: str) -> tuple[str, ...]:
    if values is None:
        return ()
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, (list, tuple, set, frozenset)):
        raise SelectionError(f"{where}: must be a list of strings")
    out: list[str] = []
    for entry in values:
        if not isinstance(entry, str) or not entry:
            raise SelectionError(f"{where}: entries must be non-empty strings")
        lowered = entry.lower()
        if lowered not in out:
            out.append(lowered)
    return tuple(out)


@dataclass(frozen=True)
class InitialRequest:
    """Bounded request for initial runtime selection.

    ``task`` is matched truncated to :data:`MAX_TASK_CHARS` so regex and
    substring conditions stay bounded; the full task still reaches the
    runtime unchanged. ``workspace`` is used only for trait detection and
    baseline comparison, never executed.
    """

    task: str = ""
    tags: tuple[str, ...] = ()
    agent: str = ""
    mode: str = ""
    workspace_kind: str = "local"
    permission_ceiling: str = "smart"
    explicit_runtime: str | None = None
    profile_pin: str | None = None
    default_runtime: str | None = None
    fallback_runtime: str | None = None
    required_capabilities: tuple[str, ...] = ()
    workspace: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "tags", _norm_set(self.tags, where="request.tags"))
        object.__setattr__(
            self,
            "required_capabilities",
            _norm_set(self.required_capabilities, where="request.required_capabilities"),
        )
        for key in (
            "agent",
            "mode",
            "workspace_kind",
            "permission_ceiling",
        ):
            value = getattr(self, key)
            if not isinstance(value, str):
                raise SelectionError(f"request.{key}: must be a string")
            # Lowercased: rule matchers normalize the same way, so "Build"
            # and "build" route identically instead of missing each other.
            object.__setattr__(self, key, value.lower())
        if self.permission_ceiling not in PERMISSION_RANK:
            raise SelectionError(
                f"request.permission_ceiling: unknown ceiling {self.permission_ceiling!r}"
            )
        if not self.workspace_kind:
            raise SelectionError("request.workspace_kind: must be a non-empty string")
        for key in ("explicit_runtime", "profile_pin", "default_runtime", "fallback_runtime"):
            value = getattr(self, key)
            if value is not None and (not isinstance(value, str) or not value):
                raise SelectionError(f"request.{key}: must be a runtime id string or None")


@dataclass(frozen=True)
class RepoTraits:
    """Bounded, side-effect-free workspace traits.

    Derived from directory entries and file extensions only — file contents
    are never read, nothing is executed, and symlinked directories are never
    followed. ``truncated`` reports when bounds cut the walk short.
    """

    languages: frozenset[str] = field(default_factory=frozenset)
    marker_files: frozenset[str] = field(default_factory=frozenset)
    files: tuple[str, ...] = ()
    file_count: int = 0
    truncated: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "languages": sorted(self.languages),
            "marker_files": sorted(self.marker_files),
            "files": list(self.files),
            "file_count": self.file_count,
            "truncated": self.truncated,
        }


@dataclass(frozen=True)
class SelectionRule:
    """One declarative routing rule naming a single runtime.

    Every non-empty matcher must hold (AND); an empty matcher is no
    constraint. ``task_regexes`` is global-trust-only — the project parser
    rejects it. ``index`` is the declaration order used after ``priority``.
    """

    rule_id: str
    runtime: str
    priority: int = 0
    index: int = 0
    trusted: bool = True
    agents: tuple[str, ...] = ()
    modes: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    capabilities: tuple[str, ...] = ()
    workspace_kinds: tuple[str, ...] = ()
    permission_ceilings: tuple[str, ...] = ()
    languages: tuple[str, ...] = ()
    marker_files: tuple[str, ...] = ()
    task_substrings: tuple[str, ...] = ()
    task_globs: tuple[str, ...] = ()
    path_globs: tuple[str, ...] = ()
    task_regexes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.rule_id or not isinstance(self.rule_id, str):
            raise SelectionError("rule: id must be a non-empty string")
        if not self.runtime or not isinstance(self.runtime, str):
            raise SelectionError(f"rule {self.rule_id!r}: runtime must be a non-empty string")
        if not isinstance(self.priority, int) or isinstance(self.priority, bool):
            raise SelectionError(f"rule {self.rule_id!r}: priority must be an int")
        for ceiling in self.permission_ceilings:
            if ceiling not in PERMISSION_RANK:
                raise SelectionError(
                    f"rule {self.rule_id!r}: unknown permission ceiling {ceiling!r}"
                )
        for pattern in self.task_regexes:
            if len(pattern) > MAX_PATTERN_CHARS:
                raise SelectionError(
                    f"rule {self.rule_id!r}: task pattern exceeds "
                    f"{MAX_PATTERN_CHARS} chars"
                )
            try:
                re.compile(pattern)
            except re.error as exc:
                raise SelectionError(
                    f"rule {self.rule_id!r}: invalid task pattern: {exc}"
                ) from exc


@dataclass(frozen=True)
class InitialCandidate:
    """One startable runtime as seen by selection.

    Health/auth are plain strings (``ok``/``degraded``/``unavailable``,
    ``authenticated``/``unauthenticated``/``expired``/``unknown``) so this
    module stays free of transport imports; enum values are accepted and
    normalized. ``workspace_kinds``/``permission_ceilings`` of ``None`` mean
    "any". Unknown auth (``unknown``) is allowed — login state stays unknown
    until a run — while logged-out runtimes are rejected before start.
    """

    runtime_id: str
    kind: str = "acp"
    available: bool = True
    health: str = "ok"
    auth: str = "unknown"
    capabilities: frozenset[str] = field(default_factory=frozenset)
    workspace_kinds: tuple[str, ...] | None = None
    permission_ceilings: tuple[str, ...] | None = None
    version: str = "unknown"
    unavailable_reason: str = ""

    def __post_init__(self) -> None:
        if not self.runtime_id or not isinstance(self.runtime_id, str):
            raise SelectionError("candidate: runtime_id must be a non-empty string")
        health = _norm(self.health).lower()
        auth = _norm(self.auth).lower()
        if health not in _HEALTH_VALUES:
            raise SelectionError(
                f"candidate {self.runtime_id!r}: unknown health {self.health!r}"
            )
        if auth not in _AUTH_VALUES:
            raise SelectionError(
                f"candidate {self.runtime_id!r}: unknown auth {self.auth!r}"
            )
        object.__setattr__(self, "health", health)
        object.__setattr__(self, "auth", auth)
        caps = _norm_set(self.capabilities, where=f"candidate {self.runtime_id}.capabilities")
        object.__setattr__(self, "capabilities", frozenset(caps))
        if self.workspace_kinds is not None:
            object.__setattr__(
                self,
                "workspace_kinds",
                _norm_set(self.workspace_kinds, where=f"candidate {self.runtime_id}.workspace_kinds") or None,
            )
        if self.permission_ceilings is not None:
            ceilings = _norm_set(
                self.permission_ceilings,
                where=f"candidate {self.runtime_id}.permission_ceilings",
            ) or None
            for ceiling in ceilings or ():
                if ceiling not in PERMISSION_RANK:
                    raise SelectionError(
                        f"candidate {self.runtime_id!r}: unknown ceiling {ceiling!r}"
                    )
            object.__setattr__(self, "permission_ceilings", ceilings)


@dataclass(frozen=True)
class GlobalSelectionConfig:
    """Parsed trusted global selection config: defaults plus ordered rules."""

    default_runtime: str | None = None
    fallback_runtime: str | None = None
    trust_project_routes: bool = False
    rules: tuple[SelectionRule, ...] = ()


@dataclass(frozen=True)
class ProjectSelectionConfig:
    """Parsed untrusted project selection config: recommendations only."""

    rules: tuple[SelectionRule, ...] = ()


_EMPTY_TRAITS = RepoTraits()
