"""The `AgentRuntime` product boundary (P0.4, issue #11).

One stable abstraction over Garuda's native loop and external coding harnesses.
An external harness is never placed behind `Model` — that would run two agent
loops against each other. Runtimes implement `AgentRuntime`; the orchestrator
above it sees only `RuntimeInfo` and `RuntimeEvent`.
"""

from garuda.runtime.events import (
    TERMINAL_LIFECYCLE_STATES,
    RuntimeEvent,
    RuntimeEventKind,
)
from garuda.runtime.protocol import (
    PROTOCOL_VERSION,
    AgentRuntime,
    AgentRuntimeError,
    AuthStatus,
    HealthStatus,
    LifecycleState,
    RuntimeCancelledError,
    RuntimeCapabilities,
    RuntimeClosedError,
    RuntimeInfo,
    RuntimeKind,
    RuntimeNotActiveError,
    RuntimeProtocolError,
    RuntimeStartError,
    RuntimeTimeoutError,
    RuntimeTransitionError,
    check_transition,
)

__all__ = [
    "PROTOCOL_VERSION",
    "TERMINAL_LIFECYCLE_STATES",
    "AgentRuntime",
    "AgentRuntimeError",
    "AuthStatus",
    "HealthStatus",
    "LifecycleState",
    "RuntimeCapabilities",
    "RuntimeCancelledError",
    "RuntimeClosedError",
    "RuntimeEvent",
    "RuntimeEventKind",
    "RuntimeInfo",
    "RuntimeKind",
    "RuntimeNotActiveError",
    "RuntimeProtocolError",
    "RuntimeStartError",
    "RuntimeTimeoutError",
    "RuntimeTransitionError",
    "check_transition",
]
