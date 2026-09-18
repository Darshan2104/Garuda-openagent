"""Opt-in policy router (P2.3, issue #49).

Recommends or selects a runtime from capability, preference, budget,
availability, quality history, and workspace policy — deterministically, with
every step of the rationale attached to the decision for logging. Unknown
costs and quotas are never treated as free; they rank below known-good
options and display as unknown. Automatic switching stays off unless the
caller passes explicit confirmation, and even then only at a safe boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class RoutingCandidate:
    runtime_id: str
    available: bool = True
    capabilities: tuple[str, ...] = ()
    cost_usd: float | None = None
    successes: int = 0
    trials: int = 0
    unavailable_reason: str = ""

    @property
    def success_rate(self) -> float | None:
        return (self.successes / self.trials) if self.trials else None


@dataclass(frozen=True)
class RoutingRequest:
    required_capabilities: tuple[str, ...] = ()
    pin: str | None = None
    budget_usd: float | None = None
    mutating: bool = True


@dataclass(frozen=True)
class RoutingDecision:
    selected: str
    ranking: tuple[str, ...]
    rationale: tuple[str, ...]
    auto_switch: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "selected": self.selected,
            "ranking": list(self.ranking),
            "rationale": list(self.rationale),
            "auto_switch": self.auto_switch,
        }


class RoutingError(ValueError):
    """No runtime satisfies the request. Carries the rationale, not just no."""


def route(
    candidates: list[RoutingCandidate], request: RoutingRequest
) -> RoutingDecision:
    """Rank deterministically and select. Pure: same input, same decision."""
    rationale: list[str] = []
    by_id = {c.runtime_id: c for c in candidates}

    if request.pin is not None:
        pinned = by_id.get(request.pin)
        if pinned is None:
            raise RoutingError(f"pinned runtime {request.pin!r} is not configured")
        if not pinned.available:
            raise RoutingError(
                f"pinned runtime {request.pin!r} is unavailable: "
                f"{pinned.unavailable_reason or 'no reason given'}"
            )
        missing = set(request.required_capabilities) - set(pinned.capabilities)
        if missing:
            raise RoutingError(
                f"pinned runtime {request.pin!r} lacks capabilities: {sorted(missing)}"
            )
        rationale.append(f"pin: {request.pin} selected explicitly by user")
        return RoutingDecision(
            selected=pinned.runtime_id,
            ranking=(pinned.runtime_id,),
            rationale=tuple(rationale),
        )

    live = [c for c in candidates if c.available]
    rationale.append(f"availability: {len(live)}/{len(candidates)} runtimes up")
    capable = [
        c for c in live
        if set(request.required_capabilities) <= set(c.capabilities)
    ]
    dropped = sorted(c.runtime_id for c in live if c not in capable)
    if dropped:
        rationale.append(f"capability: dropped {dropped} (missing {list(request.required_capabilities)})")
    if not capable:
        raise RoutingError(
            "no available runtime has capabilities "
            f"{list(request.required_capabilities)}; " + " | ".join(rationale)
        )

    affordable: list[RoutingCandidate] = []
    for candidate in capable:
        if request.budget_usd is None:
            affordable.append(candidate)
        elif candidate.cost_usd is None:
            affordable.append(candidate)
            rationale.append(
                f"budget: {candidate.runtime_id} cost unknown (not free), kept below known-good"
            )
        elif candidate.cost_usd <= request.budget_usd:
            affordable.append(candidate)
        else:
            rationale.append(
                f"budget: dropped {candidate.runtime_id} "
                f"(${candidate.cost_usd:.4f} exceeds ${request.budget_usd:.4f})"
            )
    if not affordable:
        raise RoutingError(
            f"no runtime within budget ${request.budget_usd:.4f}; " + " | ".join(rationale)
        )

    def rank_key(candidate: RoutingCandidate) -> tuple:
        known_first = 0 if candidate.cost_usd is not None else 1
        rate = candidate.success_rate
        return (
            known_first,
            -(rate if rate is not None else -1.0),
            -candidate.trials,
            candidate.runtime_id,
        )

    ranked = sorted(affordable, key=rank_key)
    rationale.append(f"quality: order {[c.runtime_id for c in ranked]}")
    if not request.mutating:
        rationale.append("workspace: read-only run, no lease contention")
    return RoutingDecision(
        selected=ranked[0].runtime_id,
        ranking=tuple(c.runtime_id for c in ranked),
        rationale=tuple(rationale),
    )


def authorize_switch(
    decision: RoutingDecision, *, confirmed: bool, source_state: str
) -> RoutingDecision:
    """Gate automatic switching: explicit confirmation plus a safe boundary.

    `source_state` is the source runtime lifecycle value; anything other than
    idle/paused refuses. Never defaults on — callers pass confirmed=True only
    from an explicit user confirmation or an approved policy.
    """
    if not confirmed:
        raise RoutingError(
            f"switch to {decision.selected} needs explicit confirmation"
        )
    if source_state not in ("idle", "paused_at_boundary"):
        raise RoutingError(
            f"switch refused mid-turn (source is {source_state}); "
            "automatic switching is boundary-only"
        )
    return RoutingDecision(
        selected=decision.selected,
        ranking=decision.ranking,
        rationale=decision.rationale + (f"switch authorized at {source_state}",),
        auto_switch=True,
    )
