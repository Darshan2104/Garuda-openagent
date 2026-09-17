"""ACP client foundation (P0.12+, issues #21-#24).

A minimal, version-pinned ACP-over-stdio client: JSON-RPC 2.0 with
newline-delimited framing, process-group lifecycle, and typed failures. No
vendor SDK — the transport owns framing and lifecycle while exposing agent
notifications and requests for the capability/authority layers above it.
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
