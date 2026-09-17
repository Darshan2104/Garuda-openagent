"""Authority tests for issue #22 (P0.13).

Capability combinations, forbidden double-authority, strict-policy refusal,
and snapshot persistence.
"""

import itertools

import pytest

from garuda.acp.authority import (
    AgentCapabilities,
    AuthorityMap,
    AuthorityOwner,
    AuthorityPolicy,
    NegotiationError,
    ToolFamily,
    negotiate,
)

_FAMILIES = [f.value for f in ToolFamily]
_POLICIES = list(AuthorityPolicy)


def _caps(families=(), mediated=(), sandbox=True) -> AgentCapabilities:
    return AgentCapabilities(
        families=frozenset(families), mediated=frozenset(mediated), sandbox=sandbox
    )


def test_single_owner_across_the_full_matrix():
    """No policy × capability combination may yield zero or two owners."""
    subsets = []
    for width in range(len(_FAMILIES) + 1):
        subsets.extend(itertools.combinations(_FAMILIES, width))
    for families in subsets:
        for mediated in subsets:
            if not set(mediated) <= set(families):
                continue
            agent = _caps(families, mediated)
            for policy in itertools.product(_POLICIES, repeat=len(_FAMILIES)):
                wants = dict(zip(_FAMILIES, policy, strict=True))
                try:
                    resolved = negotiate(wants, agent)
                except NegotiationError:
                    continue
                assert set(resolved.owners) == set(_FAMILIES)
                for family in _FAMILIES:
                    assert resolved.owner_of(family) in (
                        AuthorityOwner.GARUDA,
                        AuthorityOwner.AGENT,
                    )


def test_strict_policies_refused_when_unenforceable():
    agent = _caps(["edit"], [])
    with pytest.raises(NegotiationError, match="no mediation"):
        negotiate({"edit": AuthorityPolicy.GARUDA_ONLY}, agent)
    with pytest.raises(NegotiationError, match="lacks the family"):
        negotiate({"terminal": AuthorityPolicy.AGENT_ONLY}, agent)
    with pytest.raises(NegotiationError, match="unknown tool families"):
        negotiate({"teleport": AuthorityPolicy.AGENT_ONLY}, agent)


def test_strict_policies_honored_when_possible():
    agent = _caps(["edit", "terminal"], ["edit"])
    resolved = negotiate(
        {"edit": AuthorityPolicy.GARUDA_ONLY, "terminal": AuthorityPolicy.AGENT_ONLY},
        agent,
    )
    assert resolved.owner_of("edit") is AuthorityOwner.GARUDA
    assert resolved.owner_of("terminal") is AuthorityOwner.AGENT


def test_safe_defaults_use_the_agent_sandbox():
    """Garuda cannot mediate and has no strict order: the agent acts sandboxed."""
    agent = _caps(["terminal"], [], sandbox=True)
    resolved = negotiate({}, agent)
    assert resolved.owner_of("terminal") is AuthorityOwner.AGENT
    assert resolved.agent_sandbox is True
    unsupported = _caps([], [])
    resolved = negotiate({}, unsupported)
    assert resolved.owner_of("terminal") is AuthorityOwner.GARUDA


def test_snapshot_round_trip_preserves_the_map():
    agent = _caps(["edit", "terminal"], ["edit"])
    resolved = negotiate({"edit": AuthorityPolicy.GARUDA_ONLY}, agent)
    snapshot = resolved.to_snapshot()
    assert snapshot == frozenset({"edit=garuda", "terminal=agent", "mcp=garuda", "approval=garuda"})
    restored = AuthorityMap.from_snapshot(snapshot, agent_sandbox=True)
    assert restored.owners == resolved.owners
    assert restored.agent_sandbox is True


def test_malformed_capabilities_fail_closed():
    with pytest.raises(NegotiationError):
        AgentCapabilities.from_dict(["not", "a", "mapping"])
    with pytest.raises(NegotiationError):
        AgentCapabilities.from_dict({"families": "edit"})
    with pytest.raises(NegotiationError, match="unsupported families"):
        AgentCapabilities.from_dict({"families": ["edit"], "mediated": ["terminal"]})
    with pytest.raises(NegotiationError):
        AgentCapabilities.from_dict({"families": [], "sandbox": "yes"})
    assert AgentCapabilities.from_dict(None) == AgentCapabilities()
