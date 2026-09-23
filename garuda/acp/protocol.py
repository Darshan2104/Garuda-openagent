"""ACP wire codec and typed failures (P0.12, issue #21).

JSON-RPC 2.0 over stdio with `Content-Length` framing (headers + `\\r\\n\\r\\n`
+ body, LSP-style). The framing functions are pure so every malformed shape is
a unit test without a process.
"""

from __future__ import annotations

import json
from typing import Any

from garuda.runtime.protocol import AgentRuntimeError

#: The ACP wire subset this client speaks. Negotiated at initialize; a fake or
#: adapter speaking anything else fails closed at handshake, not mid-session.
ACP_VERSION = "0.4"

MAX_FRAME_BYTES = 16 * 1024 * 1024

#: A peer that never sends `\r\n\r\n` must not grow the reader buffer forever.
#: Headers are a handful of ASCII lines; anything beyond this without a
#: terminator is a malformed/dead peer, failed closed.
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
    """Serialize one JSON-RPC message with its Content-Length framing."""
    body = json.dumps(payload).encode("utf-8")
    return f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body


def decode_frame(buffer: bytes) -> tuple[dict[str, Any], bytes]:
    """Split one frame off `buffer`. Returns (message, rest).

    Raises `AcpProtocolError` on malformed headers, bad lengths, oversize
    frames, or invalid JSON. Raises `ValueError` when the buffer holds no
    complete frame yet — the reader's signal to read more, not a failure.
    """
    head, sep, rest = buffer.partition(b"\r\n\r\n")
    if not sep:
        if len(buffer) > MAX_HEADER_BYTES:
            raise AcpProtocolError(
                f"frame header exceeds bound of {MAX_HEADER_BYTES} bytes without terminator"
            )
        raise ValueError("incomplete frame header")
    length: int | None = None
    seen_lengths = 0
    for line in head.split(b"\r\n"):
        name, colon, value = line.partition(b":")
        if colon and name.strip().lower() == b"content-length":
            seen_lengths += 1
            if seen_lengths > 1:
                raise AcpProtocolError("duplicate Content-Length header")
            try:
                length = int(value.strip())
            except ValueError:
                raise AcpProtocolError(f"bad Content-Length: {value!r}") from None
    if length is None:
        raise AcpProtocolError("frame has no Content-Length header")
    if length < 0 or length > MAX_FRAME_BYTES:
        raise AcpProtocolError(f"frame length out of bounds: {length}")
    if len(rest) < length:
        raise ValueError("incomplete frame body")
    try:
        message = json.loads(rest[:length])
    except json.JSONDecodeError as exc:
        raise AcpProtocolError(f"frame body is not JSON: {exc}") from exc
    if not isinstance(message, dict):
        raise AcpProtocolError("JSON-RPC message must be an object")
    return message, rest[length:]
