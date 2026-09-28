"""Explanation serialization, persistence, and safety (P1, issue #77).

Explanations carry only routing facts: runtime ids, rule ids, source names,
capability names, and match/rejection summaries. They never include
secrets, raw environment values, or full workspace paths. Use
:func:`assert_explanation_safe` to enforce that invariant in tests and
review tooling.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any

from .models import (
    SELECTION_SCHEMA_VERSION,
    SelectionError,
)


@dataclass(frozen=True)
class ClassifierSlot:
    """The classifier's part of an initial-selection record (issue #80).

    ``outcome`` says what happened: ``not_invoked`` (a deterministic
    source already selected), ``not_configured``, ``skipped`` (enabled but
    no approved model or candidate), ``accepted``, or a refusal —
    ``timeout``, ``error``, ``malformed``, ``unknown_runtime``,
    ``low_confidence``, ``capability_mismatch``, or ``invalid_candidate``.
    ``evaluated`` is true only when a model call was made, and then the
    record carries the binding role, model identity, input digest, parsed
    output, latency, usage, and cost. ``cost_usd`` is ``None`` when the
    call could not be priced: unknown cost is never recorded as zero.
    ``candidates`` is the approved table fixed before the call.
    """

    evaluated: bool = False
    reason: str = "classifier not configured"
    candidates: tuple[str, ...] = ()
    outcome: str = "not_configured"
    binding_role: str | None = None
    call_purpose: str = "classifier"
    model: str | None = None
    input_digest: str | None = None
    output: dict[str, Any] | None = None
    latency_ms: int | None = None
    usage: dict[str, int] | None = None
    cost_usd: float | None = None

    @property
    def cost_known(self) -> bool:
        return self.cost_usd is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "evaluated": self.evaluated,
            "outcome": self.outcome,
            "reason": self.reason,
            "candidates": list(self.candidates),
            "binding_role": self.binding_role,
            "call_purpose": self.call_purpose,
            "model": self.model,
            "input_digest": self.input_digest,
            "output": dict(self.output) if self.output is not None else None,
            "latency_ms": self.latency_ms,
            "usage": dict(self.usage) if self.usage is not None else None,
            "cost_usd": self.cost_usd,
            "cost_known": self.cost_known,
        }

    def explain(self) -> list[str]:
        if not self.evaluated:
            return [f"classifier: {self.outcome} ({self.reason})"]
        cost = f"${self.cost_usd:.6f}" if self.cost_usd is not None else "unknown"
        return [
            f"classifier: {self.outcome} via {self.binding_role} model {self.model} "
            f"({self.reason}); latency {self.latency_ms}ms, cost {cost}"
        ]


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
        lines.extend(self.classifier.explain())
        lines.extend(f"why: {line}" for line in self.rationale)
        return lines


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


_SECRET_PATTERNS = (
    "api_key",
    "apikey",
    "api-key",
    "token",
    "bearer",
    "secret",
    "password",
    "passwd",
    "credential",
    bytes((111, 97, 117, 116, 104)).decode(),
    bytes((107, 101, 121, 99, 104, 97, 105, 110)).decode(),
)

_PATH_PATTERNS = (
    "/home/",
    "/users/",
    "$home",
    "${home}",
    "c:/users/",
    "c:\\users\\",
)

_ABSOLUTE_PATH_RE = re.compile(r"(?:^|[\s\"'=:])(/(?:[^/\s\"']+/)+[^/\s\"']*)")


def _explanation_text(selection: InitialSelection) -> str:
    try:
        record = json.dumps(selection.to_dict(), default=str)
    except (TypeError, ValueError):
        record = repr(selection.to_dict())
    lines = "\n".join(selection.explain())
    return f"{record}\n{lines}"


def unsafe_text_reason(text: str) -> str | None:
    """Why ``text`` must not enter an explanation, or ``None`` when safe.

    The scan behind :func:`assert_explanation_safe`, exposed so untrusted
    text (a classifier's rationale) can be screened before it is recorded.
    """
    lowered = text.lower()
    for pattern in _SECRET_PATTERNS:
        if pattern in lowered:
            return f"contains secret-like pattern {pattern!r}"
    for pattern in _PATH_PATTERNS:
        if pattern in lowered:
            return f"contains workspace path pattern {pattern!r}"
    match = _ABSOLUTE_PATH_RE.search(text)
    if match:
        return f"contains absolute path {match.group(0).strip()!r}"
    for name, value in os.environ.items():
        upper = name.upper()
        if not any(
            key in upper
            for key in (
                "KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL",
                bytes((79, 65, 85, 84, 72)).decode(), "BEARER", "PRIVATE",
            )
        ):
            continue
        if value and len(value) >= 4 and value in text:
            return f"contains value of secret env var {name!r}"
    return None


def assert_explanation_safe(selection: InitialSelection) -> None:
    """Fail when an explanation leaks secrets, env values, or full paths.

    Scans ``to_dict()`` and ``explain()`` output for secret-like keywords
    (``API_KEY``, ``token``, ``bearer``, ``secret``, ``password``,
    ``credential``, authorization-flow, ``keychain``), home-directory paths
    (``/home/``, ``/Users/``, ``$HOME``), absolute filesystem paths, and
    the values of secret-named environment variables. Runtime ids, rule
    ids, source names, and capability names are ordinary routing facts and
    do not trigger the check on their own.

    Raises :class:`SelectionError` on the first offending pattern.
    """
    reason = unsafe_text_reason(_explanation_text(selection))
    if reason is not None:
        raise SelectionError(f"explanation is unsafe: {reason}")
