"""Typed `AgentRuntime` capabilities, lifecycle, contracts, and failures.

The generic contract every harness — native or external — implements. Deliberately
free of any vendor or ACP types: `RuntimeKind.ACP` is a label, not an import, so a
test double or a future transport satisfies this module without the ACP client.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol, runtime_checkable

#: Version of this public schema. Bump on any breaking change; sessions persist
#: the version they were written against so migration can fail actionably.
PROTOCOL_VERSION = "0.1"


class RuntimeKind(str, Enum):
    NATIVE = "native"
    ACP = "acp"


class LifecycleState(str, Enum):
    DISCOVERED = "discovered"
    STARTING = "starting"
    IDLE = "idle"
    RUNNING = "running"
    PAUSED_AT_BOUNDARY = "paused_at_boundary"
    CANCELLING = "cancelling"
    CLOSED = "closed"
    FAILED = "failed"


_ALLOWED_TRANSITIONS: dict[LifecycleState, frozenset[LifecycleState]] = {
    LifecycleState.DISCOVERED: frozenset({LifecycleState.STARTING}),
    LifecycleState.STARTING: frozenset({LifecycleState.IDLE, LifecycleState.FAILED}),
    LifecycleState.IDLE: frozenset(
        {
            LifecycleState.RUNNING,
            LifecycleState.PAUSED_AT_BOUNDARY,
            LifecycleState.CANCELLING,
            LifecycleState.CLOSED,
        }
    ),
    LifecycleState.RUNNING: frozenset(
        {
            LifecycleState.IDLE,
            LifecycleState.PAUSED_AT_BOUNDARY,
            LifecycleState.CANCELLING,
            LifecycleState.FAILED,
        }
    ),
    LifecycleState.PAUSED_AT_BOUNDARY: frozenset(
        {
            LifecycleState.IDLE,
            LifecycleState.RUNNING,
            LifecycleState.CANCELLING,
            LifecycleState.CLOSED,
        }
    ),
    LifecycleState.CANCELLING: frozenset({LifecycleState.CLOSED, LifecycleState.FAILED}),
    LifecycleState.CLOSED: frozenset(),
    LifecycleState.FAILED: frozenset(),
}


class HealthStatus(str, Enum):
    OK = "ok"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"


class AuthStatus(str, Enum):
    AUTHENTICATED = "authenticated"
    UNAUTHENTICATED = "unauthenticated"
    EXPIRED = "expired"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class RuntimeCapabilities:
    """Declared capability names. Unknown names are kept, never rejected here.

    Negotiation of what each name *means* is P0.13's job; the protocol only needs
    a stable, inspectable set so a router can filter on names it understands and
    surface the rest as warnings.
    """

    names: frozenset[str] = field(default_factory=frozenset)

    def supports(self, name: str) -> bool:
        return name in self.names


@dataclass(frozen=True)
class RuntimeInfo:
    runtime_id: str
    kind: RuntimeKind
    version: str
    capabilities: RuntimeCapabilities = field(default_factory=RuntimeCapabilities)
    health: HealthStatus = HealthStatus.OK
    auth: AuthStatus = AuthStatus.UNKNOWN
    native_session_id: str | None = None


class AgentRuntimeError(Exception):
    """Base for every typed runtime failure. Never raises bare `Exception`."""


class RuntimeTransitionError(AgentRuntimeError):
    def __init__(self, frm: LifecycleState, to: LifecycleState):
        super().__init__(f"invalid lifecycle transition: {frm.value} -> {to.value}")
        self.frm = frm
        self.to = to


class RuntimeStartError(AgentRuntimeError):
    """Startup or resume failed; no session is active."""


class RuntimeNotActiveError(AgentRuntimeError):
    """An operation needed an active session (started, not closed)."""


class RuntimeTimeoutError(AgentRuntimeError):
    """A bounded operation exceeded its deadline."""


class RuntimeProtocolError(AgentRuntimeError):
    """Framing, event, or stream content violated the contract."""


class RuntimeCancelledError(AgentRuntimeError):
    """The operation was cancelled; terminal state is unambiguous."""


class RuntimeClosedError(AgentRuntimeError):
    """An operation was attempted on a closed runtime or its events polled late."""


def check_transition(frm: LifecycleState, to: LifecycleState) -> None:
    """Raise `RuntimeTransitionError` unless `frm -> to` is an allowed move."""
    if to not in _ALLOWED_TRANSITIONS[frm]:
        raise RuntimeTransitionError(frm, to)


@runtime_checkable
class AgentRuntime(Protocol):
    """A complete coding-agent harness: Garuda's loop or an external agent."""

    @property
    def runtime_id(self) -> str: ...

    @property
    def kind(self) -> RuntimeKind: ...

    @property
    def version(self) -> str: ...

    @property
    def state(self) -> LifecycleState: ...

    @property
    def native_session_id(self) -> str | None:
        """The runtime's own session identity. Stable across resume."""
        ...

    async def health(self) -> HealthStatus: ...

    async def auth_status(self) -> AuthStatus: ...

    async def start(self, *, task: str, session_id: str | None = None) -> RuntimeInfo:
        """Begin a session for `task`. Raises `RuntimeStartError` on failure."""
        ...

    async def resume(self, *, native_session_id: str) -> RuntimeInfo:
        """Reattach a previous session. Raises `RuntimeStartError` if unknown."""
        ...

    async def prompt(self, text: str) -> int:
        """Deliver one turn of input. Returns the turn number."""
        ...

    async def poll_events(self, cursor: int) -> tuple[list[Any], int]:
        """Events appended after `cursor`; returns them with the new cursor."""
        ...

    async def pause_at_boundary(self) -> None:
        """Pause at a safe turn boundary. Never interrupts an active turn."""
        ...

    async def permission_response(self, *, approval_id: str, allow: bool) -> None:
        """Answer a pending approval request."""
        ...

    async def cancel(self, *, reason: str = "") -> None:
        """Cancel the active turn or switch. Terminal state stays unambiguous."""
        ...

    async def close(self) -> None:
        """Shut down. Idempotent; late event emission is rejected, not stored."""
        ...
