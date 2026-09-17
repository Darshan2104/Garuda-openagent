"""ACP client foundation (P0.12+, issues #21-#24).

A minimal, version-pinned ACP-over-stdio client: JSON-RPC 2.0 with
NDJSON framing, process-group lifecycle, and typed failures. No vendor
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
    ApprovalAuditError,
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
    adapter_for_registry,
    builtin_manifest_dicts,
    discover,
    health_of,
    load_trusted_disabled,
    load_trusted_runtime_settings,
    require_acp_argv,
    shared_registry,
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
    "AcpUnavailableError",
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
    "adapter_for_manifest",
    "adapter_for_registry",
    "builtin_manifest_dicts",
    "decode_frame",
    "describe_gaps",
    "discover",
    "encode_frame",
    "health_of",
    "load_trusted_disabled",
    "load_trusted_runtime_settings",
    "negotiate",
    "require_acp_argv",
    "shared_registry",
]
