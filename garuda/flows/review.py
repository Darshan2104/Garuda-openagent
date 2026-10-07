"""Review mechanics and independence (plan task C.7, #158).

A reviewed step names its reviewer — the flow's terminal step — with
``review: {by: reviewer, max_rounds: N}``. The reviewer's ``review`` artifact
is a bounded block::

    verdict: approve        # or: changes
    findings:
    - [major] the retry loop never ends
    - [nit] a typo in the docstring

Invalid output stops the flow. A ``blocker`` or ``major`` finding requests
changes even under ``verdict: approve``. On changes, only the reviewed step
runs again — with the findings as its input — and then the reviewer;
``max_rounds: 2`` means at most three coder/reviewer pairs. The flow ends
``review_approved`` or ``review_changes_requested``: a review is never
verification.

Independence (on unless ``independent: false``): the reviewer's actual
runtime and model must differ from the reviewed role's, from every fallback
it could have run as, and from every role it consulted.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

SEVERITIES = ("blocker", "major", "minor", "nit")
MAX_FINDINGS = 50
MAX_FINDING_CHARS = 500
_FINDING = re.compile(r"^-\s*\[(blocker|major|minor|nit)\]\s*(.+)$")


class ReviewInvalid(Exception):
    pass


@dataclass
class Review:
    verdict: str  # approve | changes
    findings: list[tuple[str, str]] = field(default_factory=list)

    @property
    def approved(self) -> bool:
        return self.verdict == "approve" and not any(
            s in ("blocker", "major") for s, _ in self.findings)

    def findings_text(self) -> str:
        return "\n".join(f"- [{s}] {t}" for s, t in self.findings)


def parse(text: str) -> Review:
    lines = [line.strip() for line in (text or "").strip().splitlines() if line.strip()]
    if not lines or not lines[0].startswith("verdict:"):
        raise ReviewInvalid("the review must start with `verdict: approve` or `verdict: changes`")
    verdict = lines[0].split(":", 1)[1].strip().lower()
    if verdict not in ("approve", "changes"):
        raise ReviewInvalid(f"unknown verdict {verdict!r}")
    findings = []
    rest = lines[1:]
    if rest and rest[0] == "findings:":
        rest = rest[1:]
    for line in rest:
        match = _FINDING.match(line)
        if not match:
            raise ReviewInvalid(f"not a finding line: {line[:80]!r}")
        if len(findings) >= MAX_FINDINGS:
            raise ReviewInvalid(f"more than {MAX_FINDINGS} findings")
        findings.append((match.group(1), match.group(2)[:MAX_FINDING_CHARS]))
    if verdict == "changes" and not findings:
        raise ReviewInvalid("`verdict: changes` needs at least one finding")
    return Review(verdict, findings)


class IdentityUnresolved(Exception):
    """Configured independence lacks trusted canonical runtime evidence."""


def _registry(plan):
    from garuda.runtime.registry import RuntimeRegistry

    # Legacy manually constructed native plans have no alias evidence. Never
    # guess that an unknown reference is a distinct external runtime.
    return getattr(plan, "identity_registry", None) or RuntimeRegistry()


def _identity(registry, spec):
    from garuda.runtime.registry import RegistryError

    try:
        return registry.get(spec.get("harness")).runtime_id, spec.get("model_id")
    except RegistryError as exc:
        raise IdentityUnresolved(f"cannot prove review independence: {exc}") from exc


def identities(config: dict, role: str, plan) -> set[tuple[str, str | None]]:
    """Canonical primary/active, configured fallback and consulted identities."""
    registry = _registry(plan)
    spec = config.get("roles", {}).get(role, {})
    found = {_identity(registry, spec)}
    if plan is not None:
        found.add((plan.runtime_id, plan.model_id))
    for entry in spec.get("fallback", []):
        found.add(_identity(registry, entry))
    for consulted in spec.get("consult", []):
        other = config.get("roles", {}).get(consulted, {})
        found.add(_identity(registry, other))
    return found


def check_independent(config: dict, reviewed: str, reviewed_plan, reviewer: str,
                      reviewer_plan) -> str | None:
    """``None`` when independent, else a collision or unresolved-evidence reason."""
    try:
        mine = (reviewer_plan.runtime_id, reviewer_plan.model_id) if reviewer_plan else (
            _identity(_registry(reviewed_plan), config["roles"][reviewer]))
        theirs = identities(config, reviewed, reviewed_plan)
    except IdentityUnresolved as exc:
        return str(exc)
    if mine in theirs:
        return (f"reviewer {reviewer} would run as {mine[0]}"
                + (f" · {mine[1]}" if mine[1] else "")
                + f", an identity {reviewed} runs, falls back to or consulted")
    if reviewer in config.get("roles", {}).get(reviewed, {}).get("consult", []):
        return f"{reviewed} consulted {reviewer}"
    return None
