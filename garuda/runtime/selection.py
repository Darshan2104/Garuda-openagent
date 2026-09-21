"""Deterministic initial runtime selection (P1, issue #77).

This module picks the runtime a session *starts* on and explains why. It is
deliberately distinct from :mod:`garuda.runtime.router` (the P2 automatic
policy router, issue #49): that layer ranks candidates by cost/quality and
gates mid-run switching, while this layer resolves one initial owner from
explicit choice, profile pins, and trusted declarative rules. The type names
are disjoint on purpose (``Initial*``/``Selection*`` here versus
``Routing*`` there) so the two layers never merge by accident.

Precedence (highest first):

1. explicit runtime (SDK/API/CLI ``explicit_runtime``);
2. profile runtime pin (``profile_pin``);
3. first matching trusted deterministic rule (priority, then declaration order);
4. classifier slot — reserved for issue #80, never selects here;
5. configured default runtime;
6. built-in ``native``.

Trust model: global configuration authorizes executables and task regexes.
Project rules are untrusted recommendations unless the global config sets
``trust_project_routes``. Task regexes are rejected in project rules because
a pathological pattern could block routing. Trait detection is bounded
direct-filesystem inspection only: no shell, no subprocess, no project-code
import. Pre-start validation (availability, health, auth, capabilities,
workspace kind, permission ceiling) runs before any segment is created, and
startup fallback proceeds only when the workspace baseline is unchanged
(:mod:`garuda.workspace.diff` is the truth). Nothing here initiates a
mid-run handoff; there is no handoff API in this module by design.
"""

from __future__ import annotations

import fnmatch
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
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

#: Heavy directories skipped during the bounded walk. The walk is a routing
#: hint, not an inventory: skipping vendored trees keeps it bounded, and
#: marker checks below still probe exact paths directly.
_SKIPPED_DIRS = frozenset(
    {
        ".git",
        "node_modules",
        "__pycache__",
        ".venv",
        "venv",
        "target",
        "dist",
        "build",
        ".tox",
        ".mypy_cache",
        ".pytest_cache",
    }
)

#: Permission ceilings, strictest first. Mirrors the dashboard ceiling rank so
#: a rule author and the run policy mean the same thing by ``smart``.
PERMISSION_RANK = {"readonly": 0, "smart": 1, "auto": 2, "yolo": 3}

_HEALTH_VALUES = frozenset({"ok", "degraded", "unavailable"})
_AUTH_VALUES = frozenset({"authenticated", "unauthenticated", "expired", "unknown"})

_EXTENSION_LANGUAGES = {
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".rb": "ruby",
    ".php": "php",
    ".cs": "csharp",
    ".cpp": "cpp",
    ".cc": "cpp",
    ".cxx": "cpp",
    ".h": "c",
    ".hpp": "cpp",
    ".c": "c",
    ".swift": "swift",
    ".kt": "kotlin",
    ".scala": "scala",
    ".sh": "shell",
    ".bash": "shell",
    ".ps1": "powershell",
    ".sql": "sql",
    ".r": "r",
    ".lua": "lua",
    ".ex": "elixir",
    ".exs": "elixir",
    ".erl": "erlang",
    ".hs": "haskell",
    ".ml": "ocaml",
    ".tf": "terraform",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".toml": "toml",
    ".json": "json",
    ".md": "markdown",
    ".html": "html",
    ".css": "css",
}

_FILENAME_LANGUAGES = {
    "dockerfile": "docker",
    "makefile": "make",
    "cmakelists.txt": "cmake",
}


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
class ClassifierSlot:
    """Reserved seam for the issue #80 classifier track.

    This layer never invokes a classifier: the slot records that the
    deterministic chain produced no selection and which candidates the
    classifier may consider. Issue #80 replaces the ``evaluated=False``
    outcome with a real validated recommendation; the field names here are
    the composition point.
    """

    evaluated: bool = False
    reason: str = "classifier not implemented (reserved for #80)"
    candidates: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "evaluated": self.evaluated,
            "reason": self.reason,
            "candidates": list(self.candidates),
        }


@dataclass(frozen=True)
class InitialSelection:
    """The explained outcome of initial selection.

    ``source`` is one of ``explicit``, ``profile``, ``rule``,
    ``classifier``, ``default``, ``native``, or ``fallback``. ``matches``
    names each rule considered with its outcome, ``rejections`` names each
    candidate refused with why, and ``recommendations`` carries untrusted
    project matches that did not auto-select. Persist via :meth:`to_dict`
    before starting the runtime so failed starts stay explainable.
    """

    selected: str
    source: str
    rule_id: str | None = None
    capabilities: tuple[str, ...] = ()
    candidates: tuple[str, ...] = ()
    matches: tuple[str, ...] = ()
    rejections: tuple[str, ...] = ()
    recommendations: tuple[str, ...] = ()
    rationale: tuple[str, ...] = ()
    classifier: ClassifierSlot = field(default_factory=ClassifierSlot)
    fallback_from: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SELECTION_SCHEMA_VERSION,
            "selected": self.selected,
            "source": self.source,
            "rule_id": self.rule_id,
            "capabilities": list(self.capabilities),
            "candidates": list(self.candidates),
            "matches": list(self.matches),
            "rejections": list(self.rejections),
            "recommendations": list(self.recommendations),
            "rationale": list(self.rationale),
            "classifier": self.classifier.to_dict(),
            "fallback_from": self.fallback_from,
        }

    def explain(self) -> list[str]:
        """Human-readable lines for a future ``--route-explain`` surface."""
        lines = [f"selected {self.selected} via {self.source}"]
        if self.rule_id:
            lines.append(f"rule: {self.rule_id}")
        lines.extend(f"match: {line}" for line in self.matches)
        lines.extend(f"rejected: {line}" for line in self.rejections)
        lines.extend(f"recommendation: {line}" for line in self.recommendations)
        if self.fallback_from:
            lines.append(f"fallback from: {self.fallback_from}")
        lines.extend(f"why: {line}" for line in self.rationale)
        return lines


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


# --- Trait detection --------------------------------------------------------


def detect_repo_traits(
    workspace: str | Path,
    *,
    markers: tuple[str, ...] | list[str] = (),
    max_files: int = MAX_TRAIT_FILES,
    max_entries: int = MAX_TRAIT_ENTRIES,
    max_depth: int = MAX_TRAIT_DEPTH,
) -> RepoTraits:
    """Inspect a workspace with bounded, side-effect-free directory reads.

    Only entry names and extensions are observed: no file is opened, no
    subprocess runs, and no project code is imported. Symlinked directories
    are listed, never followed, so a link pointing outside the workspace
    cannot widen the scan. ``markers`` names extra basenames to probe with a
    direct existence check (bounded by :data:`MAX_MARKER_CHECKS`) so a rule
    can match a marker the truncated walk did not reach.
    """
    root = Path(workspace)
    marker_names = [str(m) for m in (markers or ())]
    if len(marker_names) > MAX_MARKER_CHECKS:
        raise SelectionError(
            f"trait detection: too many marker checks ({len(marker_names)} > {MAX_MARKER_CHECKS})"
        )
    languages: set[str] = set()
    root_markers: set[str] = set()
    files: list[str] = []
    entries = 0
    truncated = False
    if root.is_dir():
        stack: list[tuple[Path, int]] = [(root, 0)]
        while stack and len(files) < max_files and entries < max_entries:
            current, depth = stack.pop()
            try:
                with os.scandir(current) as handle:
                    names = sorted((entry.name, entry) for entry in handle)
            except OSError:
                continue
            for name, entry in names:
                entries += 1
                if entries > max_entries or len(files) >= max_files:
                    # The bounds stopped the scan, not the tree.
                    truncated = True
                    break
                try:
                    # Never follow symlinked directories: list, do not descend,
                    # so a link pointing outside the workspace cannot widen
                    # the scan.
                    is_dir = entry.is_dir(follow_symlinks=False)
                except OSError:
                    continue
                if is_dir:
                    if depth < max_depth and name not in _SKIPPED_DIRS:
                        stack.append((Path(entry.path), depth + 1))
                    continue
                try:
                    rel = Path(entry.path).relative_to(root).as_posix()
                except ValueError:
                    continue
                files.append(rel)
                lowered = name.lower()
                if depth == 0:
                    # Root-level basenames are the classic marker files
                    # (pyproject.toml, package.json, ...).
                    root_markers.add(lowered)
                language = _EXTENSION_LANGUAGES.get(Path(lowered).suffix)
                if language is None:
                    language = _FILENAME_LANGUAGES.get(lowered)
                if language is not None:
                    languages.add(language)
        # Work left over means the bounds stopped the scan, not the tree.
        truncated = truncated or bool(stack)
    probed: set[str] = set()
    for marker in marker_names:
        parts = Path(marker).parts
        # Direct probes are basenames only: absolute paths and ".." would
        # escape the workspace, and nested markers are already covered by
        # the walk above (or by path_globs rules).
        if not marker or Path(marker).is_absolute() or ".." in parts or len(parts) != 1:
            continue
        lowered = marker.lower()
        if lowered in root_markers:
            continue
        try:
            if (root / marker).is_file():
                probed.add(lowered)
        except OSError:
            continue
    files_sorted = tuple(sorted(files[:max_files]))
    return RepoTraits(
        languages=frozenset(sorted(languages)),
        marker_files=frozenset(sorted(root_markers | probed)),
        files=files_sorted,
        file_count=len(files_sorted),
        truncated=truncated,
    )


_EMPTY_TRAITS = RepoTraits()


# --- Rule parsing -----------------------------------------------------------

_WHEN_ALIASES = {
    "agents": {"agents", "agent"},
    "modes": {"modes", "mode"},
    "tags": {"tags", "tag", "task_tags"},
    "capabilities": {"capabilities", "capability", "required_capabilities"},
    "workspace_kinds": {"workspace_kinds", "workspace_kind"},
    "permission_ceilings": {
        "permission_ceilings",
        "permission_ceiling",
        "ceilings",
        "ceiling",
    },
    "languages": {"languages", "language"},
    "marker_files": {"marker_files", "marker_file", "markers", "marker"},
    "task_substrings": {"task_substrings", "task_substring", "substrings", "substring"},
    "task_globs": {"task_globs", "task_glob", "globs", "glob"},
    "path_globs": {"path_globs", "path_glob", "file_globs", "file_glob"},
    "task_regexes": {
        "task_regexes",
        "task_regex",
        "regexes",
        "regex",
        "patterns",
        "pattern",
        "task_patterns",
        "task_pattern",
    },
}

_RULE_TOP_FIELDS = frozenset({"id", "priority", "runtime", "when"})
_FORBIDDEN_RULE_FIELDS = frozenset({"command", "executables", "api_base", "credentials"})


def _canonical_when_key(key: str) -> str | None:
    for canonical, aliases in _WHEN_ALIASES.items():
        if key in aliases:
            return canonical
    return None


def parse_selection_rules(
    data: object,
    *,
    trusted: bool,
    start_index: int = 0,
    source: str = "selection rules",
) -> list[SelectionRule]:
    """Parse declarative rules. Unknown or secret-carrying keys fail closed.

    ``trusted=False`` (project config) rejects ``task_regexes``: an
    untrusted pattern could stall routing. Project rules also cannot carry
    executable-shaped keys.
    """
    if data is None:
        return []
    if not isinstance(data, list):
        raise SelectionError(f"{source}: must be a list of rules")
    if len(data) > MAX_RULES:
        raise SelectionError(f"{source}: too many rules ({len(data)} > {MAX_RULES})")
    rules: list[SelectionRule] = []
    for offset, item in enumerate(data):
        where = f"{source}[{offset}]"
        if not isinstance(item, dict):
            raise SelectionError(f"{where}: must be a mapping")
        forbidden = set(item) & _FORBIDDEN_RULE_FIELDS
        if forbidden:
            raise SelectionError(
                f"{where}: executable-carrying keys are forbidden: {sorted(forbidden)}"
            )
        unknown = set(item) - _RULE_TOP_FIELDS
        if unknown:
            raise SelectionError(f"{where}: unknown fields {sorted(unknown)}")
        rule_id = item.get("id", f"rule-{start_index + offset}")
        if not isinstance(rule_id, str) or not rule_id:
            raise SelectionError(f"{where}.id: must be a non-empty string")
        runtime = item.get("runtime")
        if not isinstance(runtime, str) or not runtime:
            raise SelectionError(f"{where}.runtime: must be a non-empty string")
        priority = item.get("priority", 0)
        if not isinstance(priority, int) or isinstance(priority, bool):
            raise SelectionError(f"{where}.priority: must be an int")
        when = item.get("when", {})
        if not isinstance(when, dict):
            raise SelectionError(f"{where}.when: must be a mapping")
        buckets: dict[str, tuple[str, ...]] = {}
        for raw_key, raw_value in when.items():
            canonical = _canonical_when_key(str(raw_key))
            if canonical is None:
                raise SelectionError(f"{where}.when.{raw_key}: unknown condition")
            if canonical in buckets:
                raise SelectionError(
                    f"{where}.when: duplicate condition for {canonical!r}"
                )
            if canonical == "task_regexes" and not trusted:
                raise SelectionError(
                    f"{where}.when.{raw_key}: task patterns are global-trust-only"
                )
            values = _norm_set(raw_value, where=f"{where}.when.{raw_key}")
            if canonical == "task_regexes":
                if len(values) > MAX_PATTERNS_PER_RULE:
                    raise SelectionError(
                        f"{where}.when.{raw_key}: too many patterns "
                        f"({len(values)} > {MAX_PATTERNS_PER_RULE})"
                    )
                for pattern in values:
                    if len(pattern) > MAX_PATTERN_CHARS:
                        raise SelectionError(
                            f"{where}.when.{raw_key}: pattern exceeds "
                            f"{MAX_PATTERN_CHARS} chars"
                        )
                    try:
                        re.compile(pattern)
                    except re.error as exc:
                        raise SelectionError(
                            f"{where}.when.{raw_key}: invalid pattern: {exc}"
                        ) from exc
            buckets[canonical] = values
        rules.append(
            SelectionRule(
                rule_id=rule_id,
                runtime=runtime,
                priority=priority,
                index=start_index + offset,
                trusted=trusted,
                agents=buckets.get("agents", ()),
                modes=buckets.get("modes", ()),
                tags=buckets.get("tags", ()),
                capabilities=buckets.get("capabilities", ()),
                workspace_kinds=buckets.get("workspace_kinds", ()),
                permission_ceilings=buckets.get("permission_ceilings", ()),
                languages=buckets.get("languages", ()),
                marker_files=tuple(m.lower() for m in buckets.get("marker_files", ())),
                task_substrings=buckets.get("task_substrings", ()),
                task_globs=buckets.get("task_globs", ()),
                path_globs=buckets.get("path_globs", ()),
                task_regexes=buckets.get("task_regexes", ()),
            )
        )
    return rules


def parse_global_selection(data: object, *, source: str = "global selection") -> GlobalSelectionConfig:
    """Parse trusted global selection config (already-loaded mapping)."""
    if data is None:
        return GlobalSelectionConfig()
    if not isinstance(data, dict):
        raise SelectionError(f"{source}: must be a mapping")
    known = {"default_runtime", "fallback_runtime", "trust_project_routes", "rules"}
    if isinstance(data.get("routing"), dict):
        block = data["routing"]
    elif any(key in data for key in known):
        block = data
    else:
        # No routing section: global settings carry unrelated harness
        # settings alongside, so absence means defaults, not an error.
        return GlobalSelectionConfig()
    if not isinstance(block, dict):
        raise SelectionError(f"{source}.routing: must be a mapping")
    # The global settings file carries unrelated harness settings; only the
    # routing block is strict, and unknown routing keys fail closed.
    unknown = set(block) - known
    if unknown:
        raise SelectionError(f"{source}.routing: unknown fields {sorted(unknown)}")
    for key in ("default_runtime", "fallback_runtime"):
        value = block.get(key)
        if value is not None and (not isinstance(value, str) or not value):
            raise SelectionError(f"{source}.routing.{key}: must be a runtime id string")
    trust = block.get("trust_project_routes", False)
    if not isinstance(trust, bool):
        raise SelectionError(f"{source}.routing.trust_project_routes: must be a bool")
    rules = parse_selection_rules(
        block.get("rules"), trusted=True, source=f"{source}.routing.rules"
    )
    return GlobalSelectionConfig(
        default_runtime=block.get("default_runtime"),
        fallback_runtime=block.get("fallback_runtime"),
        trust_project_routes=trust,
        rules=tuple(sort_selection_rules(rules)),
    )


def parse_project_selection(
    data: object, *, source: str = "project selection"
) -> ProjectSelectionConfig:
    """Parse untrusted project selection config (already-loaded mapping)."""
    if data is None:
        return ProjectSelectionConfig()
    if not isinstance(data, dict):
        raise SelectionError(f"{source}: must be a mapping")
    block: Any = data.get("routing", data)
    if not isinstance(block, dict):
        raise SelectionError(f"{source}.routing: must be a mapping")
    forbidden = set(block) & _FORBIDDEN_RULE_FIELDS
    if forbidden:
        raise SelectionError(
            f"{source}.routing: executable-carrying keys are forbidden: "
            f"{sorted(forbidden)}"
        )
    unknown = set(block) - {"rules"}
    if unknown:
        raise SelectionError(f"{source}.routing: unknown fields {sorted(unknown)}")
    rules = parse_selection_rules(
        block.get("rules"), trusted=False, source=f"{source}.routing.rules"
    )
    return ProjectSelectionConfig(rules=tuple(sort_selection_rules(rules)))


def sort_selection_rules(rules: list[SelectionRule] | tuple[SelectionRule, ...]) -> list[SelectionRule]:
    """Order by descending priority, then declaration order. Deterministic."""
    return sorted(rules, key=lambda rule: (-rule.priority, rule.index))


# --- Matching ---------------------------------------------------------------


def rule_matches_request(
    rule: SelectionRule, request: InitialRequest, traits: RepoTraits
) -> tuple[bool, str]:
    """Check the request/trait matchers of one rule (candidate-agnostic).

    Returns ``(matched, detail)`` where detail names the first failing
    condition or summarizes the match. Capability and target-runtime checks
    happen in :func:`select_initial`, which sees the candidate pool.
    """
    if rule.agents and request.agent.lower() not in rule.agents:
        return False, f"agent {request.agent!r} not in {sorted(rule.agents)}"
    if rule.modes and request.mode not in rule.modes:
        return False, f"mode {request.mode!r} not in {sorted(rule.modes)}"
    if rule.tags and not (set(rule.tags) & set(request.tags)):
        return False, f"tags {sorted(request.tags)} lack any of {sorted(rule.tags)}"
    if rule.workspace_kinds and request.workspace_kind not in rule.workspace_kinds:
        return (
            False,
            f"workspace {request.workspace_kind!r} not in {sorted(rule.workspace_kinds)}",
        )
    if rule.permission_ceilings and request.permission_ceiling not in rule.permission_ceilings:
        return (
            False,
            f"ceiling {request.permission_ceiling!r} not in {sorted(rule.permission_ceilings)}",
        )
    if rule.languages and not (set(rule.languages) & set(traits.languages)):
        return False, f"languages {sorted(traits.languages)} lack any of {sorted(rule.languages)}"
    if rule.marker_files and not set(rule.marker_files) <= set(traits.marker_files):
        missing = sorted(set(rule.marker_files) - set(traits.marker_files))
        return False, f"marker files missing {missing}"
    task_head = request.task[:MAX_TASK_CHARS].lower()
    if rule.task_substrings and not any(
        sub in task_head for sub in rule.task_substrings
    ):
        return False, f"task lacks any of {len(rule.task_substrings)} substrings"
    if rule.task_globs and not any(
        fnmatch.fnmatchcase(task_head, pattern) for pattern in rule.task_globs
    ):
        return False, f"task matches none of {len(rule.task_globs)} globs"
    if rule.path_globs:
        pairs = [(path, Path(path).name.lower()) for path in traits.files]
        matched = any(
            fnmatch.fnmatchcase(path, pattern) or fnmatch.fnmatchcase(base, pattern)
            for pattern in rule.path_globs
            for path, base in pairs
        )
        if not matched:
            return False, f"workspace files match none of {len(rule.path_globs)} globs"
    if rule.task_regexes:
        head = request.task[:MAX_TASK_CHARS]
        try:
            matched = any(re.search(pattern, head) for pattern in rule.task_regexes)
        except re.error as exc:
            return False, f"task pattern failed safely: {exc}"
        if not matched:
            return False, f"task matches none of {len(rule.task_regexes)} patterns"
    return True, f"rule {rule.rule_id!r} matches for runtime {rule.runtime!r}"


def validate_candidate(
    candidate: InitialCandidate, request: InitialRequest
) -> tuple[bool, str]:
    """Pre-start validation: health, auth, capabilities, workspace, policy.

    Anything ambiguous fails closed with a reason; :func:`select_initial`
    records the reason instead of starting the runtime.
    """
    if not candidate.available:
        return False, f"{candidate.runtime_id}: unavailable ({candidate.unavailable_reason or 'no reason given'})"
    if candidate.health == "unavailable":
        return False, f"{candidate.runtime_id}: health is unavailable"
    if candidate.auth in ("unauthenticated", "expired"):
        return False, f"{candidate.runtime_id}: auth is {candidate.auth}"
    missing = set(request.required_capabilities) - set(candidate.capabilities)
    if missing:
        return False, f"{candidate.runtime_id}: lacks capabilities {sorted(missing)}"
    if candidate.workspace_kinds is not None and request.workspace_kind not in candidate.workspace_kinds:
        return (
            False,
            f"{candidate.runtime_id}: does not support workspace {request.workspace_kind!r}",
        )
    if (
        candidate.permission_ceilings is not None
        and request.permission_ceiling not in candidate.permission_ceilings
    ):
        return (
            False,
            f"{candidate.runtime_id}: does not support ceiling {request.permission_ceiling!r}",
        )
    return True, ""


# --- Selection ----------------------------------------------------------------


def _candidate_table(
    candidates: list[InitialCandidate] | tuple[InitialCandidate, ...],
) -> dict[str, InitialCandidate]:
    table: dict[str, InitialCandidate] = {}
    for candidate in candidates:
        if candidate.runtime_id in table:
            raise SelectionError(f"duplicate candidate {candidate.runtime_id!r}")
        table[candidate.runtime_id] = candidate
    return table


def select_initial(
    request: InitialRequest,
    candidates: list[InitialCandidate] | tuple[InitialCandidate, ...],
    *,
    rules: list[SelectionRule] | tuple[SelectionRule, ...] = (),
    project_rules: list[SelectionRule] | tuple[SelectionRule, ...] = (),
    traits: RepoTraits | None = None,
    trust_project_routes: bool = False,
) -> InitialSelection:
    """Resolve one initial runtime with a complete explanation.

    Pure: same inputs select the same runtime. Untrusted project rules are
    recommendations unless ``trust_project_routes`` (a global-config grant)
    is true, in which case they run after the trusted rules in the same
    priority order. The classifier slot is recorded, never invoked.
    """
    if not candidates:
        raise SelectionError("selection needs at least one candidate")
    table = _candidate_table(candidates)
    traits = traits if traits is not None else _EMPTY_TRAITS
    ordered = sort_selection_rules(list(rules))
    ordered_project = sort_selection_rules(list(project_rules))
    candidate_ids = tuple(sorted(table))
    matches: list[str] = []
    rejections: list[str] = []
    rationale: list[str] = []
    recommendations: list[str] = []

    def _validated(runtime_id: str, *, where: str) -> InitialCandidate | None:
        candidate = table.get(runtime_id)
        if candidate is None:
            rejections.append(f"{where}: unknown runtime {runtime_id!r}")
            rationale.append(f"{where}: unknown runtime {runtime_id!r}, refused")
            return None
        ok, reason = validate_candidate(candidate, request)
        if not ok:
            rejections.append(f"{where}: {reason}")
            rationale.append(f"{where}: {reason}")
            return None
        return candidate

    def _finish(candidate: InitialCandidate, *, source: str, rule_id: str | None) -> InitialSelection:
        rationale.append(f"{source}: {candidate.runtime_id} selected")
        return InitialSelection(
            selected=candidate.runtime_id,
            source=source,
            rule_id=rule_id,
            capabilities=tuple(sorted(candidate.capabilities)),
            candidates=candidate_ids,
            matches=tuple(matches),
            rejections=tuple(rejections),
            recommendations=tuple(recommendations),
            rationale=tuple(rationale),
            classifier=ClassifierSlot(
                evaluated=False,
                reason="classifier not implemented (reserved for #80)",
                candidates=candidate_ids,
            ),
        )

    if request.explicit_runtime is not None:
        candidate = table.get(request.explicit_runtime)
        if candidate is None:
            raise SelectionError(
                f"explicit runtime {request.explicit_runtime!r} is not configured"
            )
        ok, reason = validate_candidate(candidate, request)
        if not ok:
            raise SelectionError(f"explicit runtime {request.explicit_runtime!r}: {reason}")
        rationale.append(f"explicit: {request.explicit_runtime} selected explicitly")
        return _finish(candidate, source=SOURCE_EXPLICIT, rule_id=None)

    if request.profile_pin is not None:
        candidate = table.get(request.profile_pin)
        if candidate is None:
            raise SelectionError(
                f"profile pin {request.profile_pin!r} is not configured"
            )
        ok, reason = validate_candidate(candidate, request)
        if not ok:
            raise SelectionError(f"profile pin {request.profile_pin!r}: {reason}")
        rationale.append(f"profile: {request.profile_pin} selected by profile pin")
        return _finish(candidate, source=SOURCE_PROFILE, rule_id=None)

    for rule in ordered:
        matched, detail = rule_matches_request(rule, request, traits)
        if not matched:
            matches.append(f"{rule.rule_id} -> {rule.runtime}: no ({detail})")
            continue
        matches.append(f"{rule.rule_id} -> {rule.runtime}: yes ({detail})")
        if rule.capabilities:
            target = table.get(rule.runtime)
            if target is None:
                rejections.append(f"rule {rule.rule_id}: unknown runtime {rule.runtime!r}")
                rationale.append(f"rule {rule.rule_id}: unknown runtime, skipped")
                continue
            missing = set(rule.capabilities) - set(target.capabilities)
            if missing:
                rejections.append(
                    f"rule {rule.rule_id}: {rule.runtime} lacks {sorted(missing)}"
                )
                rationale.append(f"rule {rule.rule_id}: target incapable, skipped")
                continue
        candidate = _validated(rule.runtime, where=f"rule {rule.rule_id}")
        if candidate is None:
            continue
        rationale.append(f"rule: {rule.rule_id} (priority {rule.priority}) selected {candidate.runtime_id}")
        return _finish(candidate, source=SOURCE_RULE, rule_id=rule.rule_id)
    rationale.append(
        f"rules: {len(ordered)} trusted rules considered, "
        f"{sum(1 for m in matches if ': yes' in m)} matched"
    )

    for rule in ordered_project:
        matched, detail = rule_matches_request(rule, request, traits)
        entry = f"{rule.rule_id} -> {rule.runtime}: {detail}"
        if not matched:
            recommendations.append(f"no: {entry}")
            continue
        recommendations.append(f"project rule {entry}")
        if not trust_project_routes:
            continue
        rationale.append(f"project rule: {rule.rule_id} authorized by global config")
        if rule.capabilities:
            target = table.get(rule.runtime)
            if target is None:
                rejections.append(
                    f"project rule {rule.rule_id}: unknown runtime {rule.runtime!r}"
                )
                continue
            missing = set(rule.capabilities) - set(target.capabilities)
            if missing:
                rejections.append(
                    f"project rule {rule.rule_id}: {rule.runtime} lacks {sorted(missing)}"
                )
                continue
        candidate = _validated(rule.runtime, where=f"project rule {rule.rule_id}")
        if candidate is None:
            continue
        rationale.append(
            f"project rule: {rule.rule_id} (priority {rule.priority}) "
            f"selected {candidate.runtime_id}"
        )
        return _finish(candidate, source=SOURCE_RULE, rule_id=rule.rule_id)
    if ordered_project and not trust_project_routes:
        rationale.append(
            "project: recommendations only (global config did not authorize "
            "project routing)"
        )

    rationale.append("classifier: slot reserved for #80, no selection made")

    if request.default_runtime is not None:
        candidate = _validated(request.default_runtime, where="default")
        if candidate is not None:
            rationale.append(f"default: {request.default_runtime} selected")
            return _finish(candidate, source=SOURCE_DEFAULT, rule_id=None)
        rationale.append("default: configured default refused, continuing to native")

    native = table.get(BUILTIN_NATIVE_ID)
    if native is not None:
        ok, reason = validate_candidate(native, request)
        if ok:
            rationale.append("native: built-in fallback selected")
            return _finish(native, source=SOURCE_NATIVE, rule_id=None)
        rejections.append(f"native: {reason}")
        rationale.append(f"native: {reason}")
    raise SelectionError(
        "no initial runtime available: " + " | ".join(rationale + rejections)
    )


# --- Startup fallback -------------------------------------------------------


def workspace_unchanged(baseline_before: object, workspace: str | Path) -> tuple[bool, str]:
    """Recheck the workspace baseline before a startup fallback.

    Uses :mod:`garuda.workspace.diff` as the truth: any non-preexisting
    change since ``baseline_before`` blocks automatic fallback, because the
    failed start may have mutated the tree. Outside a repository (or when
    git cannot answer) the check fails closed and blocks fallback.
    """
    try:
        from garuda.workspace import diff as workspace_diff
    except ImportError as exc:
        return False, f"baseline recheck unavailable: {exc}"
    try:
        if hasattr(baseline_before, "commit"):
            baseline = baseline_before  # type: ignore[assignment]
        else:
            baseline = workspace_diff.Baseline.from_dict(dict(baseline_before))  # type: ignore[arg-type]
        if not baseline.commit:
            # No baseline commit means the session started outside a
            # repository (or git could not answer then): there is nothing
            # to compare against, so fallback is refused rather than
            # allowed unverified.
            return False, "no baseline commit to compare against"
        delta = workspace_diff.session_delta(baseline, workspace)
    except Exception as exc:
        return False, f"baseline recheck failed closed: {exc}"
    if delta.changed:
        return False, f"workspace changed since baseline: {sorted(delta.changed)[:8]}"
    return True, "workspace matches baseline"


def select_startup_fallback(
    selection: InitialSelection,
    request: InitialRequest,
    candidates: list[InitialCandidate] | tuple[InitialCandidate, ...],
    *,
    baseline_before: object,
    workspace: str | Path,
) -> InitialSelection:
    """Choose the configured startup fallback after a failed start.

    Exactly one fallback is attempted, and only when ``workspace_unchanged``
    holds: an unexplained delta means the failed start may have written, so
    automatic retry on another runtime would pile work onto a dirty tree.
    The fallback target is ``request.fallback_runtime`` (validated like any
    selection) or built-in native when unset. The original selection is kept
    in ``fallback_from`` and the rationale.
    """
    unchanged, reason = workspace_unchanged(baseline_before, workspace)
    if not unchanged:
        raise SelectionError(f"startup fallback refused: {reason}")
    table = _candidate_table(candidates)
    target_id = request.fallback_runtime or BUILTIN_NATIVE_ID
    if target_id == selection.selected:
        raise SelectionError(
            f"startup fallback refused: {target_id!r} already failed to start"
        )
    candidate = table.get(target_id)
    if candidate is None:
        raise SelectionError(f"startup fallback {target_id!r} is not configured")
    ok, why = validate_candidate(candidate, request)
    if not ok:
        raise SelectionError(f"startup fallback {target_id!r}: {why}")
    rationale = tuple(selection.rationale) + (
        f"fallback: {selection.selected} failed to start; {reason}",
        f"fallback: {target_id} selected",
    )
    return InitialSelection(
        selected=candidate.runtime_id,
        source=SOURCE_FALLBACK,
        rule_id=None,
        capabilities=tuple(sorted(candidate.capabilities)),
        candidates=tuple(sorted(table)),
        matches=selection.matches,
        rejections=tuple(selection.rejections),
        recommendations=selection.recommendations,
        rationale=rationale,
        classifier=selection.classifier,
        fallback_from=selection.selected,
    )


# --- Persistence ------------------------------------------------------------


def record_initial_selection(store: object, session_id: str, selection: InitialSelection) -> None:
    """Persist the selection record onto a session before the runtime starts.

    Uses the existing atomic locked meta path (``update_meta``), so no
    session-schema change is needed: the record lands under
    ``initial_selection`` and survives inside the unified document's
    preserved fields. Failed starts therefore stay explainable.
    """
    update_meta = getattr(store, "update_meta", None)
    if not callable(update_meta):
        raise SelectionError("session store has no update_meta path")
    update_meta(session_id, {"initial_selection": selection.to_dict()})


def load_initial_selection(meta: dict[str, Any]) -> dict[str, Any] | None:
    """Read back a persisted selection record, or ``None`` when absent."""
    record = meta.get("initial_selection")
    return dict(record) if isinstance(record, dict) else None
