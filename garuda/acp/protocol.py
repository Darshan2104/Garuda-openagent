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
