"""ACP v1 wire codec and typed failures (P0.12, issue #21).

ACP stdio uses one JSON-RPC value per newline-delimited line. Keeping the
codec pure makes malformed, partial, oversized, and non-object messages
testable without spawning a process.
"""

from __future__ import annotations

import json
from typing import Any

from garuda.runtime.protocol import AgentRuntimeError

#: ACP v1 is negotiated at initialize; mismatches fail before sessions start.
ACP_VERSION = 1

MAX_FRAME_BYTES = 16 * 1024 * 1024

#: A peer that never sends `\r\n\r\n` must not grow the reader buffer forever.
#: Headers are a handful of ASCII lines; anything beyond this without a
#: terminator is a malformed/dead peer, failed closed.
# Kept as a compatibility name for callers that used the old framing bound.
# It now bounds an unterminated NDJSON line rather than a header block.
MAX_HEADER_BYTES = 16 * 1024


class AcpError(AgentRuntimeError):
    """Base for every typed ACP transport failure."""


class AcpTimeoutError(AcpError):
    """A bounded call exceeded its deadline."""


class AcpProtocolError(AcpError):
    """Framing, JSON, or envelope violated the contract."""


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
        if len(buffer) > MAX_HEADER_BYTES:
            raise AcpProtocolError(
                f"frame exceeds bound of {MAX_HEADER_BYTES} bytes without newline"
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
