"""Which ACP adapters may be given the ``consult`` MCP tool (plan task G.3, #170).

The tool is exposed to an external asker only for an adapter identity (the package
name and version it reported at ``initialize``) whose G.1 capture proves **all three**:
the adapter forwards a stdio MCP server from ``session/new``, a permission request
names the MCP server and tool in structured form, and a documented handshake stops
every caller workspace operation while a snapshot is taken. Each is recorded
independently as ``supported``, ``declared`` or ``unknown``; only ``supported`` counts,
and an identity with no record at all is ``unknown``.

Today no adapter qualifies: both captured adapters forward the server but prove neither
permission provenance nor quiescence, so ACP consult initiation stays disabled. That
does not affect native askers. Adding a row here is a reviewed change backed by a
capture under ``tests/fixtures/consult/`` (``scripts/capture_consult_transport.py``);
a live adapter claim also needs separately authorised spend.
"""

from __future__ import annotations

from dataclasses import dataclass

SUPPORTED = "supported"
DECLARED = "declared"
UNKNOWN = "unknown"


@dataclass(frozen=True)
class TransportGate:
    package: str
    version: str
    forwarding: str = UNKNOWN
    permission_provenance: str = UNKNOWN
    quiescence: str = UNKNOWN
    evidence: str = ""

    def outcomes(self) -> dict[str, str]:
        return {"forwarding": self.forwarding,
                "permission_provenance": self.permission_provenance,
                "quiescence": self.quiescence}


@dataclass(frozen=True)
class Exposure:
    exposed: bool
    reason: str
    code: str = "consult.transport_unsupported"


GATES: tuple[TransportGate, ...] = (
    TransportGate("@agentclientprotocol/claude-agent-acp", "0.85.0", forwarding=SUPPORTED,
                  evidence="tests/fixtures/consult/claude-0.85.0.json"),
    TransportGate("@agentclientprotocol/codex-acp", "2.1.1", forwarding=SUPPORTED,
                  evidence="tests/fixtures/consult/codex-2.1.1.json"),
)


#: Per exact adapter identity: the proved quiescence handshake (a callable that stops every
#: caller workspace operation, or raises) and the extractor that reads the structured
#: ``(mcp server, tool)`` out of a permission request. Both are empty because no capture has
#: proved either; an identity needs a gate row **and** an entry in each to be exposed.
QUIESCE_HANDSHAKES: dict[tuple[str, str], object] = {}
PERMISSION_IDENTITY: dict[tuple[str, str], object] = {}


class TransportPolicy:
    """The gate table plus the per-adapter implementations, injectable for tests."""

    def __init__(self, gates=GATES, quiesce=None, identity=None):
        self.gates = tuple(gates)
        self.quiesce = QUIESCE_HANDSHAKES if quiesce is None else quiesce
        self.identity = PERMISSION_IDENTITY if identity is None else identity

    def gate_for(self, package: str, version: str) -> TransportGate | None:
        for gate in self.gates:
            if gate.package == package and gate.version == version:
                return gate
        return None

    def exposure(self, package: str, version: str) -> Exposure:
        """Whether this exact adapter identity may be given the tool. Fail closed."""
        package, version = str(package or ""), str(version or "")
        gate = self.gate_for(package, version)
        if gate is None:
            return Exposure(False, f"no transport record for {package or 'an unnamed adapter'} "
                                   f"{version or '(no version)'}")
        missing = [name for name, outcome in gate.outcomes().items() if outcome != SUPPORTED]
        if missing:
            return Exposure(False, f"{gate.package} {gate.version} has not proved "
                                   + ", ".join(missing))
        key = (package, version)
        if key not in self.quiesce or key not in self.identity:
            return Exposure(False, f"{gate.package} {gate.version} has no quiescence handshake "
                                   "or permission-identity reader implemented")
        return Exposure(True, "all three transport gates are supported for this adapter")


DEFAULT = TransportPolicy()


def gate_for(package: str, version: str) -> TransportGate | None:
    return DEFAULT.gate_for(package, version)


def exposure(package: str, version: str) -> Exposure:
    return DEFAULT.exposure(package, version)
