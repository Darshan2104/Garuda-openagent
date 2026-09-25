"""Capability negotiation and execution authority (P0.13, issue #22).

For each tool family (editing, terminal, MCP, approvals) exactly one party —
Garuda or the agent — owns execution. Single ownership is structural: an
`AuthorityMap` holds one owner per family, so Garuda can never duplicate an
agent tool call by consulting the map. Strict policies (`GARUDA_ONLY`,
`AGENT_ONLY`) are refused when the agent cannot honor them; otherwise safe
defaults apply — when Garuda cannot mediate, the agent's own sandbox owns
execution, and when the agent cannot act, Garuda does.

The map persists through the unified session's capability snapshot as
`"<family>=<owner>"` names, so a resumed session carries the negotiated
assignment. What it records is negotiated *intent*, not an enforced boundary:
`families`/`mediated`/`sandbox` are Garuda extension fields an agent declares
about itself (standard ACP v1 agents send none), the sandbox flag is
unverified, and because Garuda advertises no `fs`/`terminal` client
capability, a v1 agent executes its own tools whatever the map says.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from garuda.acp.protocol import AcpError


class ToolFamily(str, Enum):
    EDIT = "edit"
    TERMINAL = "terminal"
    MCP = "mcp"
    APPROVAL = "approval"


class AuthorityOwner(str, Enum):
    GARUDA = "garuda"
    AGENT = "agent"


class AuthorityPolicy(str, Enum):
    GARUDA_ONLY = "garuda_only"
    GARUDA_PREFERRED = "garuda_preferred"
    AGENT_PREFERRED = "agent_preferred"
    AGENT_ONLY = "agent_only"


_ALL_FAMILIES = tuple(ToolFamily)


class NegotiationError(AcpError):
    """A strict policy the agent cannot honor, or malformed capabilities."""


@dataclass(frozen=True)
class AgentCapabilities:
    """What the agent declared at initialize: supported families, the subset
    Garuda may mediate, and whether agent-owned execution is sandboxed."""

    families: frozenset[str] = frozenset()
    mediated: frozenset[str] = frozenset()
    sandbox: bool = False

    @classmethod
    def from_dict(cls, data: object, *, where: str = "agentCapabilities") -> "AgentCapabilities":
        if data is None:
            return cls()
        if not isinstance(data, dict):
            raise NegotiationError(f"{where}: must be a mapping")
        families = _names(data.get("families", []), where=f"{where}.families")
        mediated = _names(data.get("mediated", []), where=f"{where}.mediated")
        unknown_mediated = mediated - families
        if unknown_mediated:
            raise NegotiationError(
                f"{where}.mediated names unsupported families: {sorted(unknown_mediated)}"
            )
        sandbox = data.get("sandbox", False)
        if not isinstance(sandbox, bool):
            raise NegotiationError(f"{where}.sandbox: must be a boolean")
        return cls(families=families, mediated=mediated, sandbox=sandbox)


def _names(value: object, *, where: str) -> frozenset[str]:
    if not isinstance(value, list) or any(not isinstance(v, str) for v in value):
        raise NegotiationError(f"{where}: must be a list of names")
    return frozenset(value)


@dataclass(frozen=True)
class AuthorityMap:
    """Exactly one owner per family. Consult `owner_of` — never both toolchains.

    Validated on every construction path (`__post_init__`): all four families
    must be present with exactly one valid owner each. Incomplete maps,
    invalid owners, and dual ownership fail closed with `NegotiationError` —
    especially at the restore boundary (`from_snapshot`), where inventing
    owners would silently fork authority.
    """

    owners: dict[str, str] = field(default_factory=dict)
    agent_sandbox: bool = False

    def __post_init__(self) -> None:
        expected = {f.value for f in _ALL_FAMILIES}
        actual = set(self.owners)
        if actual != expected:
            missing = sorted(expected - actual)
            unknown = sorted(actual - expected)
            problems = []
            if missing:
                problems.append(f"missing families: {missing}")
            if unknown:
                problems.append(f"unknown families: {unknown}")
            raise NegotiationError(
                "authority map must hold exactly one owner per family"
                + (" (" + "; ".join(problems) + ")" if problems else "")
            )
        for family, owner in self.owners.items():
            try:
                AuthorityOwner(owner)
            except ValueError:
                raise NegotiationError(
                    f"authority map {family!r}: invalid owner {owner!r}"
                ) from None
        if not isinstance(self.agent_sandbox, bool):
            raise NegotiationError("authority map agent_sandbox: must be a boolean")

    def owner_of(self, family: str | ToolFamily) -> AuthorityOwner:
        name = family.value if isinstance(family, ToolFamily) else family
        return AuthorityOwner(self.owners[name])

    def to_snapshot(self) -> frozenset[str]:
        """Capability-snapshot names persisting this map in a session segment."""
        return frozenset(f"{family}={owner}" for family, owner in self.owners.items())

    @classmethod
    def from_snapshot(cls, names: frozenset[str], *, agent_sandbox: bool = False) -> "AuthorityMap":
        """Restore a map persisted by `to_snapshot`. Fail-closed.

        Malformed entries (no `=`, unknown family, invalid owner), duplicate
        families (dual ownership), and incomplete maps all raise
        `NegotiationError` — the restore boundary never invents owners.
        """
        if not isinstance(names, (frozenset, set)):
            raise NegotiationError("authority snapshot: must be a set of names")
        owners: dict[str, str] = {}
        valid_families = {f.value for f in _ALL_FAMILIES}
        for name in names:
            if not isinstance(name, str):
                raise NegotiationError(f"authority snapshot entry {name!r}: must be a string")
            family, sep, owner = name.partition("=")
            if not sep or family not in valid_families:
                raise NegotiationError(
                    f"authority snapshot entry {name!r}: unknown family or malformed"
                )
            try:
                AuthorityOwner(owner)
            except ValueError:
                raise NegotiationError(
                    f"authority snapshot entry {name!r}: invalid owner"
                ) from None
            if family in owners:
                raise NegotiationError(
                    f"authority snapshot: dual ownership for {family!r}"
                )
            owners[family] = AuthorityOwner(owner).value
        return cls(owners=owners, agent_sandbox=agent_sandbox)


def negotiate(
    policy: dict[str, AuthorityPolicy | str],
    agent: AgentCapabilities,
) -> AuthorityMap:
    """Assign one owner per family. Strict policies fail closed when unenforceable."""
    unknown = set(policy) - {f.value for f in _ALL_FAMILIES}
    if unknown:
        raise NegotiationError(f"unknown tool families in policy: {sorted(unknown)}")
    owners: dict[str, str] = {}
    for family in _ALL_FAMILIES:
        name = family.value
        raw_want = policy.get(name, AuthorityPolicy.AGENT_PREFERRED)
        try:
            want = AuthorityPolicy(raw_want)
        except (TypeError, ValueError):
            raise NegotiationError(
                f"unknown authority policy for {name!r}: {raw_want!r}"
            ) from None
        supported = name in agent.families
        mediated = name in agent.mediated
        # Approval interaction does not execute workspace actions itself; the
        # sandbox requirement applies to edit/terminal/MCP execution families.
        sandbox_safe = agent.sandbox or family is ToolFamily.APPROVAL
        if want is AuthorityPolicy.GARUDA_ONLY:
            if supported and not mediated:
                raise NegotiationError(
                    f"strict {want.value} for {name!r} refused: agent offers no mediation"
                )
            owners[name] = AuthorityOwner.GARUDA.value
        elif want is AuthorityPolicy.AGENT_ONLY:
            if not supported:
                raise NegotiationError(
                    f"strict {want.value} for {name!r} refused: agent lacks the family"
                )
            owners[name] = AuthorityOwner.AGENT.value
        elif want is AuthorityPolicy.GARUDA_PREFERRED:
            if mediated or not supported:
                owners[name] = AuthorityOwner.GARUDA.value
            elif sandbox_safe:
                owners[name] = AuthorityOwner.AGENT.value
            else:
                raise NegotiationError(
                    f"safe default for {name!r} refused: agent offers neither "
                    "mediation nor sandboxed execution"
                )
        else:
            if supported and sandbox_safe:
                owners[name] = AuthorityOwner.AGENT.value
            elif mediated or not supported:
                owners[name] = AuthorityOwner.GARUDA.value
            else:
                raise NegotiationError(
                    f"safe default for {name!r} refused: agent offers neither "
                    "sandboxed execution nor mediation"
                )
    return AuthorityMap(owners=owners, agent_sandbox=agent.sandbox)
