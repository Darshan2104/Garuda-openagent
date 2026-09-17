"""Context validation and secret redaction (P0.11, issue #19).

The last gate before generated context reaches `.context/` or a handoff view:
size limits, repository-path validity, secret-pattern redaction, safe
command-output summaries, and the no-reasoning policy. Unsafe handoffs fail
with an actionable error so the switch transaction blocks instead of carrying
secrets across the boundary.

Redaction only ever transforms generated pack text held in memory. It takes
strings, never paths, so durable repository documentation cannot be altered —
silently or otherwise — by this module.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

MAX_BODY_CHARS = 32_768
MAX_OUTPUT_CHARS = 2_000

#: (kind, pattern). Conservative on purpose: a false positive costs one
#: rephrase, a false negative leaks a credential into the context pack.
_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("private-key", re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")),
    ("api-token", re.compile(r"\bsk-[A-Za-z0-9_-]{8,}")),
    ("api-token", re.compile(r"\bghp_[A-Za-z0-9]{8,}")),
    ("api-token", re.compile(r"\bgho_[A-Za-z0-9]{8,}")),
    ("api-token", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{8,}")),
    ("api-token", re.compile(r"\bxox[bpas]-[A-Za-z0-9-]{8,}")),
    ("aws-key", re.compile(r"\bAKIA[0-9A-Z]{16}")),
    ("credential", re.compile(r"(?i)\b(password|passwd|api_?key|secret|token)\b\s*[:=]\s*\S+")),
    ("bearer", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/-]{8,}")),
)

#: Markers that betray raw chain-of-thought in a handoff body. Best-effort:
#: the pack compiler never emits these, so their presence means foreign text.
_REASONING_MARKERS = ("<thinking>", "<reasoning>", "chain-of-thought", "chain of thought")


@dataclass(frozen=True)
class RedactionFinding:
    kind: str
    count: int


@dataclass(frozen=True)
class ValidationResult:
    valid: bool
    errors: tuple[str, ...] = ()
    findings: tuple[RedactionFinding, ...] = ()


class UnsafeHandoffError(ValueError):
    """A handoff failed validation. Carries every violation, not just the first."""


def redact_text(text: str) -> tuple[str, list[RedactionFinding]]:
    """Replace secret patterns with `[REDACTED:<kind>]`. Returns text + findings."""
    findings: list[RedactionFinding] = []
    for kind, pattern in _SECRET_PATTERNS:

        def _mask(match: re.Match[str], kind: str = kind) -> str:
            findings.append(RedactionFinding(kind=kind, count=1))
            return f"[REDACTED:{kind}]"

        text = pattern.sub(_mask, text)
    return text, findings


def summarize_command_output(output: str, *, limit: int = MAX_OUTPUT_CHARS) -> str:
    """Clip command output to a safe summary, keeping head and tail."""
    if len(output) <= limit:
        return output
    head = limit * 2 // 3
    tail = limit - head
    clipped = len(output) - limit
    return output[:head] + f"\n[… clipped {clipped} chars]\n" + output[-tail:]


def _check_paths(files: tuple[str, ...]) -> list[str]:
    errors = []
    for path in files:
        if not path or path.startswith("/") or path.startswith("~"):
            errors.append(f"changed_files: {path!r} must be workspace-relative")
        elif ".." in path.split("/"):
            errors.append(f"changed_files: {path!r} escapes the workspace")
    return errors


def validate_pack(
    frontmatter: dict,
    body: str,
    *,
    changed_files: tuple[str, ...] = (),
) -> ValidationResult:
    """Validate a compiled pack document. Pure; reports everything at once."""
    errors: list[str] = []
    if len(body) > MAX_BODY_CHARS:
        errors.append(f"body: {len(body)} chars exceeds bound of {MAX_BODY_CHARS}")
    errors.extend(_check_paths(changed_files))
    lowered = body.lower()
    for marker in _REASONING_MARKERS:
        if marker in lowered:
            errors.append(f"body: reasoning marker {marker!r} violates the no-reasoning policy")
            break
    scanned = " ".join(
        str(v) for v in frontmatter.values() if isinstance(v, (str, int, float, bool))
    ) + "\n" + body
    _, findings = redact_text(scanned)
    kinds = {f.kind for f in findings}
    if kinds:
        errors.append(f"secrets detected ({sorted(kinds)}): redact before publishing")
    return ValidationResult(valid=not errors, errors=tuple(errors), findings=tuple(findings))


def assert_safe_for_switch(
    frontmatter: dict,
    body: str,
    *,
    changed_files: tuple[str, ...] = (),
) -> None:
    """Block a switch on an unsafe handoff with every violation listed."""
    result = validate_pack(frontmatter, body, changed_files=changed_files)
    if not result.valid:
        raise UnsafeHandoffError(
            "unsafe handoff blocks switching: " + "; ".join(result.errors)
        )


def redact_pack(
    frontmatter: dict, body: str
) -> tuple[dict, str, tuple[RedactionFinding, ...]]:
    """Redact secrets in a compiled pack. Returns (frontmatter, body, findings).

    Only string values are scanned; structure is preserved exactly. Callers set
    the schema `redacted` flag from whether findings are non-empty.
    """
    findings: list[RedactionFinding] = []

    def _redact_value(value: object) -> object:
        if not isinstance(value, str):
            return value
        cleaned, found = redact_text(value)
        findings.extend(found)
        return cleaned

    cleaned_front = {k: _redact_value(v) for k, v in frontmatter.items()}
    cleaned_body, found = redact_text(body)
    findings.extend(found)
    totals: dict[str, int] = {}
    for finding in findings:
        totals[finding.kind] = totals.get(finding.kind, 0) + 1
    return (
        cleaned_front,
        cleaned_body,
        tuple(RedactionFinding(kind=k, count=c) for k, c in sorted(totals.items())),
    )


def redact_string_list(values: tuple[str, ...]) -> tuple[tuple[str, ...], tuple[RedactionFinding, ...]]:
    """Redact each entry of a schema list field (evidence, blockers, ...)."""
    out: list[str] = []
    totals: dict[str, int] = {}
    for value in values:
        cleaned, found = redact_text(value)
        out.append(cleaned)
        for finding in found:
            totals[finding.kind] = totals.get(finding.kind, 0) + 1
    return tuple(out), tuple(
        RedactionFinding(kind=k, count=c) for k, c in sorted(totals.items())
    )
