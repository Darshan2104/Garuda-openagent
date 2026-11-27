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

#: Credential words an assignment's key must end in. A strong word names a
#: credential on its own; a weak word is also ordinary English or a shell name
#: (`tests pass:`, `secret: rotate this`, `PWD=/home/me`).
_STRONG_WORDS = (r"password|passwd|passphrase|api[_-]?key|access[_-]?key(?:[_-]?id)?"
                 r"|secret[_-]?key|private[_-]?key|client[_-]?secret"
                 r"|(?:auth|access|refresh)[_-]?token")
_WEAK_WORDS = r"pass|pwd|secret|token"

#: (kind, pattern). Conservative on purpose: a false positive costs one
#: rephrase, a false negative leaks a credential into the context pack.
#: Best-effort pattern redaction (not a secrecy proof): multiline PEM blocks,
#: common token shapes, and `key: value` credential assignments are covered;
#: arbitrary base64 blobs or novel secret formats are not. A `credential` match
#: is kept only when `_credential_value` judges its value a possible secret.
_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    # Full PEM block first (DOTALL): header + body + footer redacted as one.
    # The header-only pattern below remains as a fallback for truncated blocks.
    (
        "private-key",
        re.compile(
            r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----",
            re.DOTALL,
        ),
    ),
    ("private-key", re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")),
    ("private-key", re.compile(r"-----END [A-Z0-9 ]*PRIVATE KEY-----")),
    ("api-token", re.compile(r"\bsk-[A-Za-z0-9_-]{8,}")),
    ("api-token", re.compile(r"\bgh[psuro]_[A-Za-z0-9]{8,}")),
    ("api-token", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{8,}")),
    ("api-token", re.compile(r"\bhf_[A-Za-z0-9]{30,}")),
    ("api-token", re.compile(r"\bglpat-[A-Za-z0-9_-]{8,}")),
    ("api-token", re.compile(r"\bAIza[A-Za-z0-9_-]{8,}")),
    # An npm token is `npm_` + 36 characters; `npm_lifecycle_event` is not one.
    ("api-token", re.compile(r"\bnpm_[A-Za-z0-9]{30,}")),
    ("api-token", re.compile(r"\bxox[bpas]-[A-Za-z0-9-]{8,}")),
    ("aws-key", re.compile(r"\bAKIA[0-9A-Z]{16}")),
    ("url-credential", re.compile(r"(?i)\b[a-z][a-z0-9+.-]*://[^\s/:@]+:[^\s/@]+@")),
    # The key is a whole name (never the tail of `bypass` or `compass`) whose
    # last part is a credential word; a Python/TypeScript annotation before
    # `=` is skipped. `_credential_value` then judges the value (#870).
    ("credential", re.compile(
        rf"""(?<![A-Za-z0-9_.-])(?P<key>[A-Za-z0-9_.-]*?(?i:{_STRONG_WORDS}|{_WEAK_WORDS}))"""
        r"""(?![A-Za-z0-9_])["']?"""
        r"""(?:[ \t]*:[ \t]*[A-Za-z_][\w.]*(?:\[[^\]\n=]*\])?"""
        r"""(?:[ \t]*\|[ \t]*[A-Za-z_][\w.]*(?:\[[^\]\n=]*\])?)*(?=[ \t]*=(?!=)))?"""
        r"""[ \t]*(?P<sep>[:=])(?!=)[ \t]*"""
        r"""(?P<value>"[^"\n]+"|'[^'\n]+'|[^\s\[\]{}"',;=]+)"""
    )),
    ("credential", re.compile(
        r"""(?i)(?P<key>--password)(?P<sep>\s)\s*(?P<value>"[^"\n]+"|'[^'\n]+'|[^\s\[\]"']+)""")),
    ("basic-auth", re.compile(r"(?i)\bauthorization:\s*basic\s+[A-Za-z0-9+/]+=*")),
    # A bearer token has a digit or a dot, or is long: "bearer authentication" is prose.
    ("bearer", re.compile(r"(?i)\bbearer\s+(?:(?=[A-Za-z_~+/-]*[0-9.][A-Za-z0-9])[A-Za-z0-9._~+/-]{8,}"
                          r"|[A-Za-z0-9._~+/-]{20,})")),
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


_REGISTERED: set[str] = set()
_MIN_REGISTERED = 16  # a shorter literal would be too ambiguous to blank out of arbitrary text


def register_secret(value: str) -> None:
    """Have ``redact_text`` blank this exact literal wherever it appears.

    For values minted at run time (a per-session endpoint token) that no pattern
    could recognise. Register **before** the value is placed in any structure that
    might be logged or exported. Anything shorter than 16 characters is refused."""
    if len(value) < _MIN_REGISTERED:
        raise ValueError("a registered secret must be at least 16 characters")
    _REGISTERED.add(value)


def forget_secret(value: str) -> None:
    _REGISTERED.discard(value)


_CREDENTIAL_WORD = re.compile(rf"(?i)(?:{_STRONG_WORDS}|{_WEAK_WORDS})\Z")
_STRONG_WORD = re.compile(rf"(?i)(?:{_STRONG_WORDS})\Z")
_ANY_CREDENTIAL_WORD = re.compile(rf"(?i)(?:{_STRONG_WORDS}|{_WEAK_WORDS})")
#: Values that are a type, a literal keyword or a placeholder, not a secret.
_NOT_SECRET = frozenset((
    "str", "string", "bytes", "int", "bool", "boolean", "number", "any", "object",
    "optional", "secretstr", "secretbytes", "none", "null", "nil", "undefined",
    "true", "false", "required", "redacted",
))
_REFERENCE = re.compile(r"\$(?:[{(]|[A-Za-z_]\w*\Z)|%[A-Za-z_]\w*%\Z|<[^<>\n]*>\Z|\{\{")
_CALL = re.compile(r"[A-Za-z_][\w.]*\(")
_DOTTED = re.compile(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+\Z")
_IDENTIFIER = re.compile(r"[A-Za-z_]\w*\Z")


def _looks_secret(value: str) -> bool:
    """High-entropy shape: mixed case with a digit, or long and digit-rich."""
    if len(value) < 8 or value[0] in "/~." or any(c.isspace() for c in value):
        return False
    digits = sum(c.isdigit() for c in value)
    if digits and any(c.islower() for c in value) and any(c.isupper() for c in value):
        return True
    return len(value) >= 16 and digits >= len(value) / 5 and any(c.isalpha() for c in value)


def _credential_value(match: re.Match[str]) -> bool:
    """Whether a `credential` match assigns what may be a real secret (#870).

    The key must name a credential: a strong word (`password`, `api_key`, ...)
    or a weak one (`pass`, `pwd`, `secret`, `token`), alone or ending a
    compound name (`DB_PASS`, `x-api-key`, `self.token`, `dbPassword`,
    `PGPASSWORD`), never the tail of a word (`bypass`, `compass`). The value is
    let through when it is code rather than a literal: a type
    (`password: str`), a call or index (`getpass()`, `os.environ["X"]`), a
    reference (`$DB_PASSWORD`, `${{ secrets.X }}`, `settings.SECRET_KEY`,
    `password=password`) or a placeholder (`None`, `<password>`). A bare weak
    word before `:` (`tests pass: 3 passed`, `secret: rotate this`), a weak
    word glued to capitals (`XPASS`, `BYPASS=1`) and a bare `pwd`
    (`PWD=/home/me`) need a quoted or high-entropy value. Everything else fails
    toward redacting, so `password: <any word>` is still redacted.
    """
    key, sep, value = match.group("key"), match.group("sep"), match.group("value")
    found = _CREDENTIAL_WORD.search(key)
    if found is None:
        return False
    prefix, word = key[:found.start()], found.group()
    glued = bool(prefix) and prefix.isupper() and word.isupper() and prefix[-1] not in "_.-"
    if not prefix or glued:
        compound = False  # password, TOKEN; glued capitals: PGPASSWORD, XPASS, BYPASS
    elif prefix[-1] in "_.-" or (prefix[-1].islower() or prefix[-1].isdigit()) and word[0].isupper():
        compound = True  # DB_PASS, self.token, x-api-key, dbPassword
    else:
        return False  # bypass, compass, Passport
    quoted = value[0] in "\"'"
    inner = value[1:-1].strip() if quoted else value.rstrip(")")
    if (not any(c.isalnum() for c in inner) or inner.startswith("[REDACTED")
            or _REFERENCE.match(inner)):
        return False
    if quoted:
        return True
    following = match.string[match.end():match.end() + 1]
    if (inner.lower() in _NOT_SECRET or set(inner.lower()) <= set("x*.")
            or following in ("(", "[") or _CALL.match(inner)):
        return False
    secret_shaped = _looks_secret(inner)
    if not secret_shaped and (_DOTTED.match(inner) or _IDENTIFIER.match(inner)
                              and _ANY_CREDENTIAL_WORD.search(inner)
                              and ("_" in inner or inner.lower() == word.lower()
                                   or re.search("[a-z][A-Z]", inner))):
        return False  # settings.SECRET_KEY, password=password, token=self_token
    if not _STRONG_WORD.match(word) and not compound and (
            glued or sep == ":" or word.lower() == "pwd"):
        return secret_shaped
    return True


def _substitute(kind: str, pattern: re.Pattern[str], text: str,
                findings: list[RedactionFinding]) -> str:
    out: list[str] = []
    pos = 0
    while (match := pattern.search(text, pos)) is not None:
        if kind == "credential" and not _credential_value(match):
            # Keep the text, and look again from the value: it may hold a key.
            out.append(text[pos:match.start("value")])
            pos = match.start("value")
            continue
        out.append(text[pos:match.start()] + f"[REDACTED:{kind}]")
        findings.append(RedactionFinding(kind=kind, count=1))
        pos = match.end()
    out.append(text[pos:])
    return "".join(out)


def redact_text(text: str) -> tuple[str, list[RedactionFinding]]:
    """Replace secret patterns with `[REDACTED:<kind>]`. Returns text + findings."""
    findings: list[RedactionFinding] = []
    for literal in sorted(_REGISTERED, key=len, reverse=True):
        if literal in text:
            findings.append(RedactionFinding(kind="registered-secret", count=text.count(literal)))
            text = text.replace(literal, "[REDACTED:registered-secret]")
    for kind, pattern in _SECRET_PATTERNS:
        text = _substitute(kind, pattern, text, findings)
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

    Recursive over every persisted string *and* key: top-level fields, nested
    unknown-field mappings, lists, and tuples are all scrubbed. Structure is
    preserved exactly (same keys, same shapes, only string contents replaced).
    Callers set the schema `redacted` flag from whether findings are non-empty.
    Best-effort pattern redaction — see `_SECRET_PATTERNS`.
    """
    findings: list[RedactionFinding] = []

    def _redact_key(key: object) -> object:
        if not isinstance(key, str):
            return key
        cleaned, found = redact_text(key)
        findings.extend(found)
        return cleaned

    def _redact_value(value: object) -> object:
        if isinstance(value, str):
            cleaned, found = redact_text(value)
            findings.extend(found)
            return cleaned
        if isinstance(value, dict):
            return {_redact_key(k): _redact_value(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            cleaned = [_redact_value(v) for v in value]
            return type(value)(cleaned) if isinstance(value, tuple) else cleaned
        return value

    cleaned_front = {_redact_key(k): _redact_value(v) for k, v in frontmatter.items()}
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
