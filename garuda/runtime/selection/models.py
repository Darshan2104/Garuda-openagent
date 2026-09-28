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
#: v2 (#80): the ``classifier`` block records the optional classifier call.
SELECTION_SCHEMA_VERSION = 2

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

#: Classifier bounds (#80). One tool-free call with a small output budget:
#: the classifier recommends an id from a fixed table, it does not reason
#: at length, so a large budget only buys room for injected instructions.
CLASSIFIER_MODEL_ROLES = frozenset({"collection", "reasoning"})
CLASSIFIER_FAILURE_POLICIES = frozenset({"default"})
MAX_CLASSIFIER_OUTPUT_BUDGET = 1_024
MAX_CLASSIFIER_TIMEOUT_SEC = 120.0

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
class ClassifierPolicy:
    """Trusted global policy for the optional classifier fallback (#80).

    Disabled unless global configuration enables it. ``model_role`` names
    one of the two approved bindings; ``collection`` is the default and a
    missing collection model skips classification unless
    ``allow_reasoning_fallback`` explicitly permits the reasoning model.
    ``model_binding`` names a global ``model_bindings`` alias (``None`` uses
    the global default binding). ``candidates`` narrows the runtimes the
    classifier may recommend; ``None`` means every configured runtime.
    ``on_failure`` is ``default``: any failed or refused classification
    continues to the configured default runtime, then built-in native.
    """

    enabled: bool = False
    model_role: str = "collection"
    allow_reasoning_fallback: bool = False
    model_binding: str | None = None
    minimum_confidence: float = 0.75
    candidates: tuple[str, ...] | None = None
    max_output_tokens: int = 256
    timeout_sec: float = 20.0
    on_failure: str = "default"

    def __post_init__(self) -> None:
        if not isinstance(self.enabled, bool):
            raise SelectionError("classifier.enabled: must be a bool")
        if not isinstance(self.model_role, str) or self.model_role not in CLASSIFIER_MODEL_ROLES:
            raise SelectionError(
                f"classifier.model_role: must be one of {sorted(CLASSIFIER_MODEL_ROLES)}"
            )
        if not isinstance(self.allow_reasoning_fallback, bool):
            raise SelectionError("classifier.allow_reasoning_fallback: must be a bool")
        if self.model_binding is not None and (
            not isinstance(self.model_binding, str) or not self.model_binding
        ):
            raise SelectionError("classifier.model_binding: must be a binding alias string")
        confidence = self.minimum_confidence
        if (
            isinstance(confidence, bool)
            or not isinstance(confidence, (int, float))
            or not 0.0 <= float(confidence) <= 1.0
        ):
            raise SelectionError("classifier.minimum_confidence: must be within 0..1")
        object.__setattr__(self, "minimum_confidence", float(confidence))
        if self.candidates is not None:
            # Runtime ids are case-sensitive registry keys, so unlike rule
            # matchers they are not lowercased.
            raw = self.candidates
            if isinstance(raw, str) or not isinstance(raw, (list, tuple)):
                raise SelectionError("classifier.candidates: must be a list of runtime ids")
            ids: list[str] = []
            for entry in raw:
                if not isinstance(entry, str) or not entry:
                    raise SelectionError("classifier.candidates: entries must be non-empty strings")
                if entry not in ids:
                    ids.append(entry)
            if not ids:
                raise SelectionError("classifier.candidates: must name at least one runtime")
            object.__setattr__(self, "candidates", tuple(ids))
        budget = self.max_output_tokens
        if (
            isinstance(budget, bool)
            or not isinstance(budget, int)
            or not 0 < budget <= MAX_CLASSIFIER_OUTPUT_BUDGET
        ):
            raise SelectionError(
                "classifier.max_output_tokens: must be an int within "
                f"1..{MAX_CLASSIFIER_OUTPUT_BUDGET}"
            )
        timeout = self.timeout_sec
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not 0.0 < float(timeout) <= MAX_CLASSIFIER_TIMEOUT_SEC
        ):
            raise SelectionError(
                f"classifier.timeout_sec: must be within 0..{MAX_CLASSIFIER_TIMEOUT_SEC:g}"
            )
        object.__setattr__(self, "timeout_sec", float(timeout))
        if not isinstance(self.on_failure, str) or self.on_failure not in CLASSIFIER_FAILURE_POLICIES:
            raise SelectionError(
                f"classifier.on_failure: must be one of {sorted(CLASSIFIER_FAILURE_POLICIES)}"
            )


@dataclass(frozen=True)
class GlobalSelectionConfig:
    """Parsed trusted global selection config: defaults plus ordered rules."""

    default_runtime: str | None = None
    fallback_runtime: str | None = None
    trust_project_routes: bool = False
    rules: tuple[SelectionRule, ...] = ()
    classifier: ClassifierPolicy = field(default_factory=ClassifierPolicy)


@dataclass(frozen=True)
class ProjectSelectionConfig:
    """Parsed untrusted project selection config: recommendations only.

    ``classifier_disabled`` is the one classifier setting a project may
    carry: it can opt out of classification, never enable or widen it.
    """

    rules: tuple[SelectionRule, ...] = ()
    classifier_disabled: bool = False


_EMPTY_TRAITS = RepoTraits()
