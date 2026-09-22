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
    "oauth",
    "keychain",
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


def assert_explanation_safe(selection: InitialSelection) -> None:
    """Fail when an explanation leaks secrets, env values, or full paths.

    Scans ``to_dict()`` and ``explain()`` output for secret-like keywords
    (``API_KEY``, ``token``, ``bearer``, ``secret``, ``password``,
    ``credential``, ``oauth``, ``keychain``), home-directory paths
    (``/home/``, ``/Users/``, ``$HOME``), absolute filesystem paths, and
    the values of secret-named environment variables. Runtime ids, rule
    ids, source names, and capability names are ordinary routing facts and
    do not trigger the check on their own.

    Raises :class:`SelectionError` on the first offending pattern.
    """
    text = _explanation_text(selection)
    lowered = text.lower()
    for pattern in _SECRET_PATTERNS:
        if pattern in lowered:
            raise SelectionError(
                f"explanation is unsafe: contains secret-like pattern {pattern!r}"
            )
    for pattern in _PATH_PATTERNS:
        if pattern in lowered:
            raise SelectionError(
                f"explanation is unsafe: contains workspace path pattern {pattern!r}"
            )
    match = _ABSOLUTE_PATH_RE.search(text)
    if match:
        raise SelectionError(
            f"explanation is unsafe: contains absolute path {match.group(0).strip()!r}"
        )
    for name, value in os.environ.items():
        upper = name.upper()
        if not any(
            key in upper
            for key in ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL", "OAUTH", "BEARER", "PRIVATE")
        ):
            continue
        if value and len(value) >= 4 and value in text:
            raise SelectionError(
                f"explanation is unsafe: contains value of secret env var {name!r}"
            )
