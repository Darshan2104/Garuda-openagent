"""Rule parsing, sorting, and request matching (P1, issue #77).

Trusted declarative rules only. Trait detection lives in
:mod:`garuda.runtime.selection.constraints`; candidate validation lives
there too. This module never touches the filesystem.
"""

from __future__ import annotations

import fnmatch
import re
from pathlib import Path
from typing import Any

from .models import (
    MAX_PATTERN_CHARS,
    MAX_PATTERNS_PER_RULE,
    MAX_RULES,
    MAX_TASK_CHARS,
    GlobalSelectionConfig,
    InitialRequest,
    ProjectSelectionConfig,
    RepoTraits,
    SelectionError,
    SelectionRule,
    _norm_set,
)

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
    # Project settings share their top-level mapping with runtime references,
    # disablement suggestions, and other unrelated configuration. Only an
    # explicit routing block (or the historical rules-only shape) belongs to
    # this parser; treating unrelated keys as routing fields masks the real
    # launch gate with a misleading selection parse failure.
    if "routing" in data:
        block: Any = data["routing"]
    elif set(data) <= {"rules"}:
        block = data
    else:
        return ProjectSelectionConfig()
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


def rule_matches_request(
    rule: SelectionRule, request: InitialRequest, traits: RepoTraits
) -> tuple[bool, str]:
    """Check the request/trait matchers of one rule (candidate-agnostic).

    Returns ``(matched, detail)`` where detail names the first failing
    condition or summarizes the match. Capability and target-runtime checks
    happen in the facade's ``select_initial``, which sees the candidate pool.
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
