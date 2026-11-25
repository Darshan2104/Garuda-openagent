"""ACP v1 wire codec and typed failures (P0.12, issue #21).

ACP stdio uses one JSON-RPC value per newline-delimited line. Keeping the
codec pure makes malformed, partial, oversized, and non-object messages
testable without spawning a process.
"""

from __future__ import annotations

import json
import re
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
    when it resets (for example ``3:05 PM``), when it said.
    """

    def __init__(self, message: str, *, remote_code: int | None, detail: str = "",
                 reset_hint: str | None = None):
        super().__init__(message, remote_code=remote_code, detail=detail)
        self.reset_hint = reset_hint


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
    text = f"{message} {detail}"
    if (isinstance(info, str) and info in _USAGE_LIMIT_INFO) or (
            _USAGE_LIMIT_TEXT.search(text) and not _OS_QUOTA_TEXT.search(text)):
        # Never the generic code text: `Internal error` reads as a non-quota stop.
        cause = detail or (message if _USAGE_LIMIT_TEXT.search(message) else "usage limit reached")
        reset = _RESET_HINT.search(cause)
        return AcpUsageLimitError(
            f"harness.limit_reached: {cause} (agent error {code})",
            remote_code=code, detail=detail,
            reset_hint=reset.group(1).strip() if reset else None)
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
