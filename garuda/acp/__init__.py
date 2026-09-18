"""ACP client foundation (P0.12+, issues #21-#24) and inbound server (P2.1, #47).

A minimal, version-pinned ACP-over-stdio client: JSON-RPC 2.0 with
Content-Length framing, process-group lifecycle, and typed failures. No vendor
SDK — the wire subset Garuda needs (initialize, session/new, session/prompt,
session/cancel, session/update) is small enough to own, and owning it keeps
every framing, timeout, and cleanup path under test with fake processes.
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
    ApprovalBroker,
    ApprovalOutcome,
    ApprovalRecord,
    ApprovalRequest,
    describe_gaps,
)
from garuda.acp.catalog import (
    BUILTIN_STUBS,
    AcpUnavailableError,
    DiscoveredRuntime,
    adapter_for_manifest,
    builtin_manifest_dicts,
    discover,
    health_of,
    require_acp_argv,
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
from garuda.acp.server import AcpServer

__all__ = [
    "ACP_VERSION",
    "AcpCancelledError",
    "AcpError",
    "AcpExitError",
    "AcpNormalizer",
    "AcpProtocolError",
    "AcpRuntime",
    "AcpServer",
    "AcpTimeoutError",
    "AcpUnavailableError",
    "AgentCapabilities",
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
    "adapter_for_manifest",
    "builtin_manifest_dicts",
    "decode_frame",
    "describe_gaps",
    "discover",
    "encode_frame",
    "health_of",
    "negotiate",
    "require_acp_argv",
]
