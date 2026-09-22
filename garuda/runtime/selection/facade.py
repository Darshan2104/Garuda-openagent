"""Thin selection facade (P1, issue #77).

Owns only the ``select_initial`` precedence chain and the single-shot
startup fallback. All parsing, matching, validation, trait detection, and
explanation types live in the sibling modules; this facade delegates to
them without adding new policy.
"""

from __future__ import annotations

from pathlib import Path

from .constraints import (
    _candidate_table,
    validate_candidate,
    workspace_unchanged,
)
from .explain import ClassifierSlot, InitialSelection
from .models import (
    _EMPTY_TRAITS,
    BUILTIN_NATIVE_ID,
    SOURCE_DEFAULT,
    SOURCE_EXPLICIT,
    SOURCE_FALLBACK,
    SOURCE_NATIVE,
    SOURCE_PROFILE,
    SOURCE_RULE,
    InitialCandidate,
    InitialRequest,
    RepoTraits,
    SelectionError,
    SelectionRule,
)
from .scoring import (
    rule_matches_request,
    sort_selection_rules,
)


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
