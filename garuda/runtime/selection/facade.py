"""Thin selection facade (P1, issues #77 and #80).

Owns only the ``select_initial``/``select_initial_async`` precedence chain
and the single-shot startup fallback. All parsing, matching, validation,
trait detection, classification, and explanation types live in the sibling
modules; this facade delegates to them without adding new policy.
"""

from __future__ import annotations

from pathlib import Path

from .classifier import (
    OUTCOME_NOT_CONFIGURED,
    OUTCOME_SKIPPED,
    RuntimeClassifier,
    not_invoked_slot,
    run_classifier,
)
from .constraints import (
    _candidate_table,
    validate_candidate,
    workspace_unchanged,
)
from .explain import ClassifierSlot, InitialSelection
from .models import (
    _EMPTY_TRAITS,
    BUILTIN_NATIVE_ID,
    SOURCE_CLASSIFIER,
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


class _SelectionRun:
    """One selection attempt's shared state.

    The synchronous and asynchronous entry points run the same deterministic
    chain and the same default/native tail; only the classifier step between
    them differs, so precedence cannot drift between the two paths.
    """

    def __init__(
        self,
        request: InitialRequest,
        candidates: list[InitialCandidate] | tuple[InitialCandidate, ...],
        *,
        rules: list[SelectionRule] | tuple[SelectionRule, ...],
        project_rules: list[SelectionRule] | tuple[SelectionRule, ...],
        traits: RepoTraits | None,
        trust_project_routes: bool,
    ) -> None:
        if not candidates:
            raise SelectionError("selection needs at least one candidate")
        self.request = request
        self.table = _candidate_table(candidates)
        self.traits = traits if traits is not None else _EMPTY_TRAITS
        self.ordered = sort_selection_rules(list(rules))
        self.ordered_project = sort_selection_rules(list(project_rules))
        self.trust_project_routes = trust_project_routes
        self.candidate_ids = tuple(sorted(self.table))
        self.matches: list[str] = []
        self.rejections: list[str] = []
        self.rationale: list[str] = []
        self.recommendations: list[str] = []

    def _validated(self, runtime_id: str, *, where: str) -> InitialCandidate | None:
        candidate = self.table.get(runtime_id)
        if candidate is None:
            self.rejections.append(f"{where}: unknown runtime {runtime_id!r}")
            self.rationale.append(f"{where}: unknown runtime {runtime_id!r}, refused")
            return None
        ok, reason = validate_candidate(candidate, self.request)
        if not ok:
            self.rejections.append(f"{where}: {reason}")
            self.rationale.append(f"{where}: {reason}")
            return None
        return candidate

    def finish(
        self,
        candidate: InitialCandidate,
        *,
        source: str,
        rule_id: str | None,
        classifier: ClassifierSlot | None = None,
    ) -> InitialSelection:
        self.rationale.append(f"{source}: {candidate.runtime_id} selected")
        if classifier is None:
            classifier = not_invoked_slot(
                self.candidate_ids, reason=f"{source} selection made before the classifier"
            )
        return InitialSelection(
            selected=candidate.runtime_id,
            source=source,
            rule_id=rule_id,
            capabilities=tuple(sorted(candidate.capabilities)),
            candidates=self.candidate_ids,
            matches=tuple(self.matches),
            rejections=tuple(self.rejections),
            recommendations=tuple(self.recommendations),
            rationale=tuple(self.rationale),
            classifier=classifier,
        )

    def deterministic(self) -> InitialSelection | None:
        """Explicit, profile pin, trusted rules, then authorized project rules."""
        request = self.request
        table = self.table
        if request.explicit_runtime is not None:
            candidate = table.get(request.explicit_runtime)
            if candidate is None:
                raise SelectionError(
                    f"explicit runtime {request.explicit_runtime!r} is not configured"
                )
            ok, reason = validate_candidate(candidate, request)
            if not ok:
                raise SelectionError(f"explicit runtime {request.explicit_runtime!r}: {reason}")
            self.rationale.append(f"explicit: {request.explicit_runtime} selected explicitly")
            return self.finish(candidate, source=SOURCE_EXPLICIT, rule_id=None)

        if request.profile_pin is not None:
            candidate = table.get(request.profile_pin)
            if candidate is None:
                raise SelectionError(
                    f"profile pin {request.profile_pin!r} is not configured"
                )
            ok, reason = validate_candidate(candidate, request)
            if not ok:
                raise SelectionError(f"profile pin {request.profile_pin!r}: {reason}")
            self.rationale.append(f"profile: {request.profile_pin} selected by profile pin")
            return self.finish(candidate, source=SOURCE_PROFILE, rule_id=None)

        for rule in self.ordered:
            matched, detail = rule_matches_request(rule, request, self.traits)
            if not matched:
                self.matches.append(f"{rule.rule_id} -> {rule.runtime}: no ({detail})")
                continue
            self.matches.append(f"{rule.rule_id} -> {rule.runtime}: yes ({detail})")
            if rule.capabilities:
                target = table.get(rule.runtime)
                if target is None:
                    self.rejections.append(
                        f"rule {rule.rule_id}: unknown runtime {rule.runtime!r}"
                    )
                    self.rationale.append(f"rule {rule.rule_id}: unknown runtime, skipped")
                    continue
                missing = set(rule.capabilities) - set(target.capabilities)
                if missing:
                    self.rejections.append(
                        f"rule {rule.rule_id}: {rule.runtime} lacks {sorted(missing)}"
                    )
                    self.rationale.append(f"rule {rule.rule_id}: target incapable, skipped")
                    continue
            candidate = self._validated(rule.runtime, where=f"rule {rule.rule_id}")
            if candidate is None:
                continue
            self.rationale.append(
                f"rule: {rule.rule_id} (priority {rule.priority}) selected {candidate.runtime_id}"
            )
            return self.finish(candidate, source=SOURCE_RULE, rule_id=rule.rule_id)
        self.rationale.append(
            f"rules: {len(self.ordered)} trusted rules considered, "
            f"{sum(1 for m in self.matches if ': yes' in m)} matched"
        )

        for rule in self.ordered_project:
            matched, detail = rule_matches_request(rule, request, self.traits)
            entry = f"{rule.rule_id} -> {rule.runtime}: {detail}"
            if not matched:
                self.recommendations.append(f"no: {entry}")
                continue
            self.recommendations.append(f"project rule {entry}")
            if not self.trust_project_routes:
                continue
            self.rationale.append(f"project rule: {rule.rule_id} authorized by global config")
            if rule.capabilities:
                target = table.get(rule.runtime)
                if target is None:
                    self.rejections.append(
                        f"project rule {rule.rule_id}: unknown runtime {rule.runtime!r}"
                    )
                    continue
                missing = set(rule.capabilities) - set(target.capabilities)
                if missing:
                    self.rejections.append(
                        f"project rule {rule.rule_id}: {rule.runtime} lacks {sorted(missing)}"
                    )
                    continue
            candidate = self._validated(rule.runtime, where=f"project rule {rule.rule_id}")
            if candidate is None:
                continue
            self.rationale.append(
                f"project rule: {rule.rule_id} (priority {rule.priority}) "
                f"selected {candidate.runtime_id}"
            )
            return self.finish(candidate, source=SOURCE_RULE, rule_id=rule.rule_id)
        if self.ordered_project and not self.trust_project_routes:
            self.rationale.append(
                "project: recommendations only (global config did not authorize "
                "project routing)"
            )
        return None

    def tail(self, classifier: ClassifierSlot) -> InitialSelection:
        """Configured default, then built-in native, after no other selection."""
        request = self.request
        if request.default_runtime is not None:
            candidate = self._validated(request.default_runtime, where="default")
            if candidate is not None:
                self.rationale.append(f"default: {request.default_runtime} selected")
                return self.finish(
                    candidate, source=SOURCE_DEFAULT, rule_id=None, classifier=classifier
                )
            self.rationale.append("default: configured default refused, continuing to native")

        native = self.table.get(BUILTIN_NATIVE_ID)
        if native is not None:
            ok, reason = validate_candidate(native, request)
            if ok:
                self.rationale.append("native: built-in fallback selected")
                return self.finish(
                    native, source=SOURCE_NATIVE, rule_id=None, classifier=classifier
                )
            self.rejections.append(f"native: {reason}")
            self.rationale.append(f"native: {reason}")
        raise SelectionError(
            "no initial runtime available: " + " | ".join(self.rationale + self.rejections)
        )

    def unclassified(self, *, outcome: str, reason: str) -> ClassifierSlot:
        self.rationale.append(f"classifier: {outcome} ({reason}), no selection made")
        return ClassifierSlot(
            evaluated=False,
            outcome=outcome,
            reason=reason,
            candidates=self.candidate_ids,
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
    priority order. This synchronous path never calls a classifier; use
    :func:`select_initial_async` to run the optional classifier fallback.
    """
    run = _SelectionRun(
        request,
        candidates,
        rules=rules,
        project_rules=project_rules,
        traits=traits,
        trust_project_routes=trust_project_routes,
    )
    decided = run.deterministic()
    if decided is not None:
        return decided
    return run.tail(
        run.unclassified(outcome=OUTCOME_NOT_CONFIGURED, reason="no classifier configured")
    )


async def select_initial_async(
    request: InitialRequest,
    candidates: list[InitialCandidate] | tuple[InitialCandidate, ...],
    *,
    rules: list[SelectionRule] | tuple[SelectionRule, ...] = (),
    project_rules: list[SelectionRule] | tuple[SelectionRule, ...] = (),
    traits: RepoTraits | None = None,
    trust_project_routes: bool = False,
    classifier: RuntimeClassifier | None = None,
    classifier_skip_reason: str | None = None,
) -> InitialSelection:
    """:func:`select_initial` with the optional classifier fallback (#80).

    Precedence is explicit > profile > trusted rule > authorized project
    rule > classifier > configured default > built-in native. The
    classifier runs at most once and only when no deterministic source
    selected; a refused, malformed, timed-out, or low-confidence answer
    continues to the default. ``classifier_skip_reason`` records why an
    enabled classifier could not be bound (no approved model, disabled by
    project settings) without making a call.
    """
    run = _SelectionRun(
        request,
        candidates,
        rules=rules,
        project_rules=project_rules,
        traits=traits,
        trust_project_routes=trust_project_routes,
    )
    decided = run.deterministic()
    if decided is not None:
        return decided
    if classifier is None or not classifier.policy.enabled:
        if classifier_skip_reason:
            slot = run.unclassified(outcome=OUTCOME_SKIPPED, reason=classifier_skip_reason)
        else:
            slot = run.unclassified(
                outcome=OUTCOME_NOT_CONFIGURED, reason="no classifier configured"
            )
        return run.tail(slot)
    slot, chosen, notes = await run_classifier(classifier, request, run.table, run.traits)
    run.rationale.extend(notes)
    if chosen is not None:
        confidence = (slot.output or {}).get("confidence")
        run.rationale.append(
            f"classifier: {chosen.runtime_id} recommended at confidence "
            f"{confidence:.2f} and revalidated"
        )
        return run.finish(chosen, source=SOURCE_CLASSIFIER, rule_id=None, classifier=slot)
    run.rationale.append(f"classifier: {slot.outcome} ({slot.reason})")
    return run.tail(slot)


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
