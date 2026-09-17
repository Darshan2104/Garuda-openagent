"""Normalized runtime event vocabulary (P0.4, issue #11).

One reader renders native and external events. Every event carries its session
and turn for correlation; `kind` is the stable discriminant consumers switch on.
Raw vendor records stay in session-local diagnostics — they never appear here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class RuntimeEventKind(str, Enum):
    MESSAGE = "message"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    APPROVAL_REQUEST = "approval_request"
    LIFECYCLE = "lifecycle"
    ERROR = "error"


#: `LIFECYCLE` payload states after which no further event for the session is
#: valid. A runtime that emits past one has violated the contract.
TERMINAL_LIFECYCLE_STATES = frozenset({"closed", "failed", "cancelled"})


@dataclass(frozen=True)
class RuntimeEvent:
    kind: RuntimeEventKind
    session_id: str
    turn: int
    seq: int
    payload: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.session_id:
            raise ValueError("RuntimeEvent requires a session_id")
        if self.turn < 0:
            raise ValueError("RuntimeEvent turn must be >= 0")
        if self.seq < 0:
            raise ValueError("RuntimeEvent seq must be >= 0")

    def is_terminal(self) -> bool:
        """True for a `LIFECYCLE` event whose state ends the session."""
        return (
            self.kind is RuntimeEventKind.LIFECYCLE
            and str(self.payload.get("state", "")) in TERMINAL_LIFECYCLE_STATES
        )
