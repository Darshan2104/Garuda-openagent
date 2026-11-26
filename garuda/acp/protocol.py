"""ACP v1 wire codec and typed failures (P0.12, issue #21).

ACP stdio uses one JSON-RPC value per newline-delimited line. Keeping the
codec pure makes malformed, partial, oversized, and non-object messages
testable without spawning a process.
"""

from __future__ import annotations

import json
import math
import re
from datetime import datetime, timezone
from typing import Any

from garuda.runtime.protocol import AgentRuntimeError

#: ACP v1 is negotiated at initialize; mismatches fail before sessions start.
ACP_VERSION = 1

#: One NDJSON line — a complete JSON-RPC message — may be at most this long.
#: The same bound applies to an unterminated line still being read, so a peer
#: that never sends `\n` cannot grow the reader buffer forever, while a large
#: but legitimate message (a big diff, long tool output) still fits.
MAX_FRAME_BYTES = 16 * 1024 * 1024


class AcpError(AgentRuntimeError):
    """Base for every typed ACP transport failure."""


class AcpTimeoutError(AcpError):
    """A bounded call exceeded its deadline."""


class AcpProtocolError(AcpError):
    """Framing, JSON, or envelope violated the contract."""


class AcpRemoteError(AcpProtocolError):
    """The peer returned a JSON-RPC error response.

    It remains an ``AcpProtocolError`` for catch compatibility, while carrying
    the remote code separately so evidence never confuses it with malformed
    framing or a locally detected protocol violation.
    """

    def __init__(self, message: str, *, remote_code: int | None, detail: str = ""):
        super().__init__(message)
        self.remote_code = remote_code
        #: The bounded, one-line text of the error's ``data`` (``""`` if none).
        self.detail = detail


class AcpUsageLimitError(AcpRemoteError):
    """The harness refused a turn because the account's usage limit is reached.

    Typed ``harness.limit_reached`` so a quota stop is never read as a generic
    ``Internal error`` (#684). ``reset_hint`` is the harness's own words for
    when it resets (for example ``3:05 PM``), when it said. ``rate_limit`` is
    the structured limit the harness reported (#824): only the validated
    ``status``, ``resets_at`` (epoch seconds) and ``rate_limit_type`` of
    :func:`rate_limit_fields`, never free text.
    """

    def __init__(self, message: str, *, remote_code: int | None, detail: str = "",
                 reset_hint: str | None = None, rate_limit: dict | None = None,
                 cause: str = ""):
        super().__init__(message, remote_code=remote_code, detail=detail)
        self.reset_hint = reset_hint
        self.rate_limit = rate_limit_fields(rate_limit, validated=True)
        self._cause = cause

    @property
    def resets_at(self) -> int | None:
        """When the limit resets (epoch seconds, UTC), when the harness said."""
        return self.rate_limit.get("resets_at")

    def note_rate_limit(self, snapshot: dict | None) -> None:
        """Fill what the error itself did not carry from the session's latest
        reported limit snapshot (claude-agent-acp before 0.110 sends only
        ``errorKind`` with the failure, and the reset time in usage updates).
        The message is rebuilt so every stop message names the reset. The
        snapshot's status is its own (often ``allowed_warning``), never this
        failure's, so only the reset time and the limit window are taken."""
        fields = rate_limit_fields(snapshot, validated=True)
        if "resets_at" not in fields or "resets_at" in self.rate_limit:
            return
        taken = {key: fields[key] for key in ("resets_at", "rate_limit_type") if key in fields}
        self.rate_limit = {**taken, **self.rate_limit}
        self.args = (_limit_message(self._cause, self.rate_limit, self.remote_code),)


#: Bound on the error ``data`` text kept in a remote error's message.
MAX_ERROR_DETAIL_CHARS = 500
#: Codex's typed cause for an exhausted plan (``codexErrorInfo``).
_USAGE_LIMIT_INFO = frozenset({"usageLimitExceeded", "usage_limit_exceeded"})
_USAGE_LIMIT_TEXT = re.compile(r"usage limit|quota (?:exceeded|exhausted)|insufficient_quota", re.I)
#: An operating-system quota (EDQUOT, ``Disk quota exceeded (os error 122)``)
#: is a host failure, never an account limit (#728): free text naming one never
#: types the error. Codex's structured ``codexErrorInfo`` still does.
_OS_QUOTA_TEXT = re.compile(r"disk quota|\bos error \d+|\bEDQUOT\b|\berrno\b", re.I)
_RESET_HINT = re.compile(r"try again (?:at|in|after) ([^.;\n]{1,64})", re.I)
#: claude-agent-acp's typed cause for an account limit: ``data.errorKind``
#: (0.85.0 sends ``{"errorKind": "rate_limit"}``; 0.110.1 adds ``rateLimit``).
_CLAUDE_LIMIT_KIND = "rate_limit"
#: How Claude Code words a usage stop: the start of the CLI text that
#: claude-agent-acp puts after ``Internal error: `` (``USAGE_LIMIT_ERROR_PREFIXES``
#: of @anthropic-ai/claude-agent-sdk, as shipped with claude-agent-acp 0.85.0).
CLAUDE_LIMIT_PREFIXES = (
    "You've hit your", "You've reached your", "You're out of usage credits",
    "Your org is out of usage", "Your seat type doesn't include usage",
    "Your usage allocation has been disabled", "Your group's usage limit is set to $0",
    "You're out of extra usage", "Your seat type doesn't include extra usage",
    "Fable 5 requires usage credits",
)
_INTERNAL_ERROR_PREFIX = re.compile(r"^\s*internal error\s*:?\s*", re.I)
#: A rate-limit ``status`` or ``rateLimitType`` is a short identifier, never text.
_LIMIT_TOKEN = re.compile(r"[a-z][a-z0-9_]{0,39}")
#: A sane ``resetsAt``: 2020-01-01 to 2100-01-01 UTC, in epoch seconds.
_RESETS_AT_RANGE = (1_577_836_800, 4_102_444_800)


def rate_limit_fields(info: Any, *, validated: bool = False) -> dict[str, Any]:
    """The bounded fields of a reported rate limit, each validated or dropped.

    ``info`` is the harness's ``rateLimit`` / ``_claude/rateLimit`` object
    (``status``, ``resetsAt``, ``rateLimitType``), or, with ``validated``, a dict
    this function already returned. Anything else, including any text, is dropped.
    """
    if not isinstance(info, dict):
        return {}
    keys = (("status", "resets_at", "rate_limit_type") if validated
            else ("status", "resetsAt", "rateLimitType"))
    out: dict[str, Any] = {}
    status, resets, kind = (info.get(key) for key in keys)
    if isinstance(status, str) and _LIMIT_TOKEN.fullmatch(status):
        out["status"] = status
    if (isinstance(resets, (int, float)) and not isinstance(resets, bool)
            and math.isfinite(resets)
            and _RESETS_AT_RANGE[0] <= resets <= _RESETS_AT_RANGE[1]):
        out["resets_at"] = int(resets)
    if isinstance(kind, str) and _LIMIT_TOKEN.fullmatch(kind):
        out["rate_limit_type"] = kind
    return out


def reset_time_text(resets_at: int) -> str:
    """``2026-11-26 15:00 UTC`` for an epoch-seconds reset time."""
    return datetime.fromtimestamp(resets_at, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _limit_message(cause: str, rate_limit: dict, code: int | None) -> str:
    reset = ""
    if "resets_at" in rate_limit:
        window = rate_limit.get("rate_limit_type")
        reset = (f"; the {window} limit resets at " if window else "; resets at ") + \
            reset_time_text(rate_limit["resets_at"])
    return f"harness.limit_reached: {cause}{reset} (agent error {code})"


def _redact(text: str) -> str:
    from garuda.context.redact import redact_text

    return redact_text(text)[0]


def _error_detail(data: Any) -> str:
    """One bounded line from a JSON-RPC error's ``data``: its ``message`` if it
    has one (Codex: ``{"message", "codexErrorInfo"}``), a string as is, nothing else."""
    if isinstance(data, dict):
        data = data.get("message") or data.get("details")
    if not isinstance(data, str):
        return ""
    # Adapters put upstream error bodies here (request echoes, credentials):
    # redact before the text reaches the exception and every sink (#729).
    text = " ".join(_redact(data).split())
    if len(text) > MAX_ERROR_DETAIL_CHARS:
        text = text[: MAX_ERROR_DETAIL_CHARS - 1] + "…"
    return text


def remote_error(code: int, message: str, data: Any = None) -> AcpRemoteError:
    """The typed error for a JSON-RPC error response, keeping its ``data`` cause.

    A usage-limit cause (Codex's ``codexErrorInfo: usageLimitExceeded`` or the
    harness saying its usage limit is reached) is an ``AcpUsageLimitError``
    whose message leads with ``harness.limit_reached`` instead of the generic
    ``Internal error`` code text.
    """
    detail = _error_detail(data)
    message = _redact(message if isinstance(message, str) else str(message))
    info = data.get("codexErrorInfo") if isinstance(data, dict) else None
    kind = data.get("errorKind") if isinstance(data, dict) else None
    text = f"{message} {detail}"
    # claude-agent-acp: "Internal error: <CLI text>" (#824).
    stated = _INTERNAL_ERROR_PREFIX.sub("", message, count=1)
    claude_words = stated.startswith(CLAUDE_LIMIT_PREFIXES)
    if (isinstance(info, str) and info in _USAGE_LIMIT_INFO) or kind == _CLAUDE_LIMIT_KIND or (
            (_USAGE_LIMIT_TEXT.search(text) or claude_words) and not _OS_QUOTA_TEXT.search(text)):
        # Never the generic code text: `Internal error` reads as a non-quota stop.
        cause = detail or (stated if stated and (_USAGE_LIMIT_TEXT.search(message) or claude_words)
                           else "usage limit reached")
        reset = _RESET_HINT.search(cause)
        rate_limit = rate_limit_fields(data.get("rateLimit") if isinstance(data, dict) else None)
        return AcpUsageLimitError(
            _limit_message(cause, rate_limit, code),
            remote_code=code, detail=detail,
            reset_hint=reset.group(1).strip() if reset else None,
            rate_limit=rate_limit, cause=cause)
    suffix = f": {detail}" if detail and detail != message else ""
    return AcpRemoteError(f"agent error {code}: {message}{suffix}", remote_code=code,
                          detail=detail)


class AcpExitError(AcpError):
    """The agent process exited unexpectedly. Carries code + stderr tail."""

    def __init__(self, message: str, *, exit_code: int | None, stderr_tail: str = ""):
        super().__init__(message)
        self.exit_code = exit_code
        self.stderr_tail = stderr_tail


class AcpCancelledError(AcpError):
    """A pending call was cancelled. Terminal state stays unambiguous."""


def encode_frame(payload: dict[str, Any]) -> bytes:
    """Serialize one JSON-RPC message as a single NDJSON line."""
    if not isinstance(payload, dict):
        raise AcpProtocolError("JSON-RPC message must be an object")
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    ) + b"\n"


def decode_frame(buffer: bytes) -> tuple[dict[str, Any], bytes]:
    """Split one frame off `buffer`. Returns (message, rest).

    Raises `AcpProtocolError` on malformed, oversize, or invalid JSON lines.
    Raises `ValueError` when the buffer holds no complete line yet — the
    reader's signal to read more, not a failure.
    """
    newline = buffer.find(b"\n")
    if newline < 0:
        if len(buffer) > MAX_FRAME_BYTES:
            raise AcpProtocolError(
                f"frame exceeds bound of {MAX_FRAME_BYTES} bytes without newline"
            )
        raise ValueError("incomplete frame")
    line = buffer[:newline].rstrip(b"\r")
    rest = buffer[newline + 1 :]
    if not line:
        raise AcpProtocolError("empty ACP frame")
    if len(line) > MAX_FRAME_BYTES:
        raise AcpProtocolError(f"frame length out of bounds: {len(line)}")
    try:
        message = json.loads(line)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AcpProtocolError(f"frame is not valid JSON: {exc}") from exc
    if not isinstance(message, dict):
        raise AcpProtocolError("JSON-RPC message must be an object")
    if message.get("jsonrpc") != "2.0":
        raise AcpProtocolError("JSON-RPC message must declare jsonrpc='2.0'")
    return message, rest
