"""ACP v1 NDJSON wire codec and typed failures (P0.12, issue #21).

Stable ACP v1 uses one newline-delimited JSON-RPC object per stdio record.
The framing helpers are pure so malformed shapes are tested without a process.
"""

from __future__ import annotations

import json
from typing import Any

from garuda.runtime.protocol import AgentRuntimeError

#: The ACP wire subset this client speaks. Negotiated at initialize; a fake or
#: adapter speaking anything else fails closed at handshake, not mid-session.
ACP_VERSION = 1

MAX_FRAME_BYTES = 16 * 1024 * 1024

#: Compatibility name for the unfinished-record allowance in aggregate reader
#: bounds. ACP v1 NDJSON has no headers.
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
    """Serialize one ACP v1 JSON-RPC record as newline-delimited JSON."""
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    if len(body) > MAX_FRAME_BYTES:
        raise AcpProtocolError(f"NDJSON message exceeds bound of {MAX_FRAME_BYTES} bytes")
    return body + b"\n"


def decode_frame(buffer: bytes) -> tuple[dict[str, Any], bytes]:
    """Split one ACP v1 NDJSON record off ``buffer``.

    ``ValueError`` means more bytes are needed. Malformed, blank, and oversized
    records are protocol failures rather than data the client can skip.
    """
    body, sep, rest = buffer.partition(b"\n")
    if not sep:
        if len(buffer) > MAX_FRAME_BYTES:
            raise AcpProtocolError(
                f"NDJSON record exceeds bound of {MAX_FRAME_BYTES} bytes without newline"
            )
        raise ValueError("incomplete NDJSON record")
    if len(body) > MAX_FRAME_BYTES:
        raise AcpProtocolError(f"NDJSON record exceeds bound of {MAX_FRAME_BYTES} bytes")
    if body.endswith(b"\r"):
        body = body[:-1]
    if not body:
        raise AcpProtocolError("blank NDJSON record")
    try:
        message = json.loads(body)
    except json.JSONDecodeError as exc:
        raise AcpProtocolError(f"frame body is not JSON: {exc}") from exc
    if not isinstance(message, dict):
        raise AcpProtocolError("JSON-RPC message must be an object")
    return message, rest
