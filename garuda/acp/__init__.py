"""ACP client foundation (P0.12+, issues #21-#24).

A minimal, version-pinned ACP-over-stdio client: JSON-RPC 2.0 with
Content-Length framing, process-group lifecycle, and typed failures. No vendor
SDK — the wire subset Garuda needs (initialize, session/new, session/prompt,
session/cancel, session/update) is small enough to own, and owning it keeps
every framing, timeout, and cleanup path under test with fake processes.
"""

from garuda.acp.authority import (
    AgentCapabilities,
    AuthorityMap,
    AuthorityOwner,
    AuthorityPolicy,
    NegotiationError,
    ToolFamily,
    negotiate,
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
    "AcpTimeoutError",
    "AgentCapabilities",
    "AuthorityMap",
    "AuthorityOwner",
    "AuthorityPolicy",
    "NegotiationError",
    "ToolFamily",
    "decode_frame",
    "encode_frame",
    "negotiate",
]
