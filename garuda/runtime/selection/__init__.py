"""Deterministic initial runtime selection (P1, issue #77).

This package picks the runtime a session *starts* on and explains why. It
is deliberately distinct from :mod:`garuda.runtime.router` (the P2
automatic policy router, issue #49): that layer ranks candidates by
cost/quality and gates mid-run switching, while this layer resolves one
initial owner from explicit choice, profile pins, and trusted declarative
rules.

Split into focused modules: :mod:`models` (types and constants),
:mod:`scoring` (rule parsing and matching), :mod:`constraints` (trait
detection, candidate validation, workspace baseline),
:mod:`explain` (explanation serialization, persistence, safety), and
:mod:`facade` (the thin ``select_initial``/``select_startup_fallback``
orchestration). This file only re-exports the stable public API.
"""

from .constraints import (
    detect_repo_traits,
    validate_candidate,
    workspace_unchanged,
)
from .explain import (
    ClassifierSlot,
    InitialSelection,
    assert_explanation_safe,
    load_initial_selection,
    record_initial_selection,
)
from .facade import (
    select_initial,
    select_startup_fallback,
)
from .models import (
    BUILTIN_NATIVE_ID,
    MAX_MARKER_CHECKS,
    MAX_PATTERN_CHARS,
    MAX_PATTERNS_PER_RULE,
    MAX_RULES,
    MAX_TASK_CHARS,
    MAX_TRAIT_DEPTH,
    MAX_TRAIT_ENTRIES,
    MAX_TRAIT_FILES,
    PERMISSION_RANK,
    SELECTION_SCHEMA_VERSION,
    SOURCE_CLASSIFIER,
    SOURCE_DEFAULT,
    SOURCE_EXPLICIT,
    SOURCE_FALLBACK,
    SOURCE_NATIVE,
    SOURCE_PROFILE,
    SOURCE_RULE,
    GlobalSelectionConfig,
    InitialCandidate,
    InitialRequest,
    ProjectSelectionConfig,
    RepoTraits,
    SelectionError,
    SelectionRule,
)
from .scoring import (
    parse_global_selection,
    parse_project_selection,
    parse_selection_rules,
    rule_matches_request,
    sort_selection_rules,
)

__all__ = [
    "BUILTIN_NATIVE_ID",
    "ClassifierSlot",
    "GlobalSelectionConfig",
    "InitialCandidate",
    "InitialRequest",
    "InitialSelection",
    "MAX_MARKER_CHECKS",
    "MAX_PATTERNS_PER_RULE",
    "MAX_PATTERN_CHARS",
    "MAX_RULES",
    "MAX_TASK_CHARS",
    "MAX_TRAIT_DEPTH",
    "MAX_TRAIT_ENTRIES",
    "MAX_TRAIT_FILES",
    "PERMISSION_RANK",
    "ProjectSelectionConfig",
    "RepoTraits",
    "SELECTION_SCHEMA_VERSION",
    "SelectionError",
    "SelectionRule",
    "SOURCE_CLASSIFIER",
    "SOURCE_DEFAULT",
    "SOURCE_EXPLICIT",
    "SOURCE_FALLBACK",
    "SOURCE_NATIVE",
    "SOURCE_PROFILE",
    "SOURCE_RULE",
    "assert_explanation_safe",
    "detect_repo_traits",
    "load_initial_selection",
    "parse_global_selection",
    "parse_project_selection",
    "parse_selection_rules",
    "record_initial_selection",
    "rule_matches_request",
    "select_initial",
    "select_startup_fallback",
    "sort_selection_rules",
    "validate_candidate",
    "workspace_unchanged",
]
