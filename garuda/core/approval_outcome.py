"""How an approval request ended, shared by every channel that carries one.

The ACP session broker records these outcomes; the role-control socket carries a
non-answer across a process boundary. Kept here, outside ``garuda.acp``, so the
generic runtime layer can use them without depending on a vendor protocol.
"""

from __future__ import annotations

from enum import Enum


class ApprovalOutcome(str, Enum):
    ALLOW = "allow"
    DENY = "deny"
    TIMEOUT = "timeout"
    DISCONNECTED = "disconnected"
    CANCELLED = "cancelled"


class Unanswered:
    """A responder's non-answer: it timed out or its person went away.

    Falsy wherever it is tested, so a caller that does not know this type
    still denies; the broker records the real terminal outcome instead of
    collapsing it into a deny. (An :class:`ApprovalOutcome` would be truthy,
    being a non-empty string, and would fail open.)
    """

    __slots__ = ("outcome", "reason")

    def __init__(self, outcome: ApprovalOutcome, reason: str | None = None):
        if outcome not in (ApprovalOutcome.TIMEOUT, ApprovalOutcome.DISCONNECTED):
            raise ValueError(f"not a non-answer outcome: {outcome!r}")
        self.outcome = outcome
        self.reason = reason

    def __bool__(self) -> bool:
        return False

    def __repr__(self) -> str:
        return f"Unanswered({self.outcome.value!r})"
