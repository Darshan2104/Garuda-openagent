"""ACP client foundation (P0.12+, issues #21-#24).

A minimal, version-pinned ACP-over-stdio client: JSON-RPC 2.0 with
newline-delimited framing, process-group lifecycle, and typed failures. No
vendor SDK — the transport owns framing and lifecycle while exposing agent
notifications and requests for the capability/authority layers above it.
"""

from garuda.acp.adapter import AcpRuntime
from garuda.acp.authority import (
    AgentCapabilities,
    AuthorityMap,
    AuthorityOwner,
    AuthorityPolicy,
    NegotiationError,
    ToolFamily,
    negotiate,
)
from garuda.acp.broker import (
    ApprovalAuditError,
    ApprovalBroker,
    ApprovalOutcome,
    ApprovalRecord,
    ApprovalRequest,
    describe_gaps,
)
from garuda.acp.catalog import (
    BUILTIN_STUBS,
    DiscoveredRuntime,
    discover,
    health_of,
    load_trusted_disabled,
    load_trusted_runtime_settings,
)
from garuda.acp.normalize import AcpNormalizer
from garuda.acp.protocol import (
    ACP_VERSION,
    AcpCancelledError,
    AcpError,
    AcpExitError,
    AcpProtocolError,
    AcpTimeoutError,
    decode_frame,
    encode_frame,
)

__all__ = [
    "ACP_VERSION",
    "AcpCancelledError",
    "AcpError",
    "AcpExitError",
    "AcpNormalizer",
    "AcpProtocolError",
    "AcpRuntime",
    "AcpTimeoutError",
    "AgentCapabilities",
    "ApprovalAuditError",
    "ApprovalBroker",
    "ApprovalOutcome",
    "ApprovalRecord",
    "ApprovalRequest",
    "AuthorityMap",
    "AuthorityOwner",
    "AuthorityPolicy",
    "BUILTIN_STUBS",
    "DiscoveredRuntime",
    "NegotiationError",
    "ToolFamily",
    "decode_frame",
    "describe_gaps",
    "discover",
    "encode_frame",
    "health_of",
    "load_trusted_disabled",
    "load_trusted_runtime_settings",
    "negotiate",
]
