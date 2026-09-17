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
from garuda.runtime.registry import (
    BUILTIN_NATIVE_ID,
    ProjectRuntimeRef,
    RegistryError,
    ResolvedRuntime,
    RuntimeManifest,
    RuntimeRegistry,
    parse_global_manifests,
    parse_project_refs,
)

__all__ = [
    "BUILTIN_NATIVE_ID",
    "PROTOCOL_VERSION",
    "TERMINAL_LIFECYCLE_STATES",
    "AgentRuntime",
    "AgentRuntimeError",
    "AuthStatus",
    "HealthStatus",
    "LifecycleState",
    "ProjectRuntimeRef",
    "RegistryError",
    "ResolvedRuntime",
    "RuntimeCapabilities",
    "RuntimeCancelledError",
    "RuntimeClosedError",
    "RuntimeEvent",
    "RuntimeEventKind",
    "RuntimeInfo",
    "RuntimeKind",
    "RuntimeManifest",
    "RuntimeNotActiveError",
    "RuntimeProtocolError",
    "RuntimeRegistry",
    "RuntimeStartError",
    "RuntimeTimeoutError",
    "RuntimeTransitionError",
    "check_transition",
    "parse_global_manifests",
    "parse_project_refs",
]
