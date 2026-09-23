"""Opt-in policy router (P2.3, issue #49).

Recommends or selects a runtime from capability, preference, budget,
availability, quality history, and workspace policy — deterministically, with
every step of the rationale attached to the decision for logging. Unknown
costs and quotas are never treated as free; they rank below known-good
options and display as unknown. Automatic switching stays off unless the
caller passes explicit confirmation, and even then only at a safe boundary.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True)
class RoutingCandidate:
    runtime_id: str
    available: bool = True
    capabilities: tuple[str, ...] = ()
    cost_usd: float | None = None
    successes: int = 0
    trials: int = 0
    unavailable_reason: str = ""
    mutating_allowed: bool = True

    def __post_init__(self) -> None:
        if not self.runtime_id:
            raise RoutingError("runtime_id is required")
        if self.cost_usd is not None:
            if not isinstance(self.cost_usd, (int, float)) or isinstance(self.cost_usd, bool):
                raise RoutingError(f"{self.runtime_id}: cost_usd must be a finite number")
            if not math.isfinite(self.cost_usd):
                raise RoutingError(f"{self.runtime_id}: cost_usd must be finite")
            if self.cost_usd < 0:
                raise RoutingError(f"{self.runtime_id}: cost_usd cannot be negative")
        if self.trials < 0 or self.successes < 0 or self.successes > self.trials:
            raise RoutingError(
                f"{self.runtime_id}: quality history must satisfy 0 <= successes <= trials"
            )

    @property
    def success_rate(self) -> float | None:
        return (self.successes / self.trials) if self.trials else None


@dataclass(frozen=True)
class RoutingRequest:
    required_capabilities: tuple[str, ...] = ()
    pin: str | None = None
    budget_usd: float | None = None
    mutating: bool = True

    def __post_init__(self) -> None:
        if self.budget_usd is not None:
            if not isinstance(self.budget_usd, (int, float)) or isinstance(self.budget_usd, bool):
                raise RoutingError("budget_usd must be a finite number")
            if not math.isfinite(self.budget_usd):
                raise RoutingError("budget_usd must be finite")
            if self.budget_usd < 0:
                raise RoutingError("budget_usd cannot be negative")
        if any(not capability for capability in self.required_capabilities):
            raise RoutingError("required capabilities must be non-empty names")


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
    if len(by_id) != len(candidates):
        duplicates = sorted(
            runtime_id
            for runtime_id in by_id
            if sum(c.runtime_id == runtime_id for c in candidates) > 1
        )
        raise RoutingError(f"duplicate runtime candidates: {duplicates}")

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
        if request.mutating and not pinned.mutating_allowed:
            raise RoutingError(
                f"pinned runtime {request.pin!r} is refused by workspace mutation policy"
            )
        if (
            request.budget_usd is not None
            and pinned.cost_usd is not None
            and pinned.cost_usd > request.budget_usd
        ):
            raise RoutingError(
                f"pinned runtime {request.pin!r} costs ${pinned.cost_usd:.4f}, "
                f"over budget ${request.budget_usd:.4f}"
            )
        rationale.append(f"pin: {request.pin} selected explicitly by user")
        if request.budget_usd is not None and pinned.cost_usd is None:
            rationale.append(f"budget: {request.pin} cost unknown (not free)")
        return RoutingDecision(
            selected=pinned.runtime_id,
            ranking=(pinned.runtime_id,),
            rationale=tuple(rationale),
        )

    live = [c for c in candidates if c.available]
    rationale.append(f"availability: {len(live)}/{len(candidates)} runtimes up")
    if request.mutating:
        blocked = sorted(c.runtime_id for c in live if not c.mutating_allowed)
        live = [c for c in live if c.mutating_allowed]
        if blocked:
            rationale.append(f"workspace: dropped {blocked} (mutation not allowed)")
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


def record_routing_decision(store: object, session_id: str, decision: RoutingDecision) -> None:
    """Persist the routing decision onto a session before the runtime starts.

    Uses the existing atomic locked meta path (``update_meta``), so no
    session-schema change is needed: the record lands under
    ``routing_decision`` and survives inside the unified document.
    """
    update_meta = getattr(store, "update_meta", None)
    if not callable(update_meta):
        raise RoutingError("session store has no update_meta path")
    update_meta(session_id, {"routing_decision": decision.to_dict()})


def candidates_from_discovered(
    discovered: list[Any],
    *,
    costs: Mapping[str, float | None] | None = None,
    history: Mapping[str, tuple[int, int]] | None = None,
    mutating_allowed: Mapping[str, bool] | None = None,
) -> list[RoutingCandidate]:
    """Build routing candidates from discovery records.

    ``costs`` / ``history`` / ``mutating_allowed`` are optional overlays keyed
    by runtime id. Unknown cost stays ``None`` (never free). History defaults
    to zero trials. Mutating eligibility defaults to allowed.
    """
    costs = costs or {}
    history = history or {}
    mutating_allowed = mutating_allowed or {}
    out: list[RoutingCandidate] = []
    for entry in discovered:
        runtime_id = getattr(entry, "runtime_id", None) or entry.get("runtime_id")
        if not runtime_id:
            raise RoutingError("discovered entry missing runtime_id")
        available = bool(getattr(entry, "available", None)
                         if not isinstance(entry, dict) else entry.get("available", True))
        caps = getattr(entry, "capabilities", None)
        if caps is None and isinstance(entry, dict):
            caps = entry.get("capabilities", ())
        capabilities = tuple(caps or ())
        warnings = getattr(entry, "warnings", None)
        if warnings is None and isinstance(entry, dict):
            warnings = entry.get("warnings", ())
        reason = ""
        if not available:
            reason = "; ".join(str(w) for w in (warnings or ()) if w) or "unavailable"
        successes, trials = history.get(runtime_id, (0, 0))
        out.append(
            RoutingCandidate(
                runtime_id=runtime_id,
                available=available,
                capabilities=capabilities,
                cost_usd=costs.get(runtime_id),
                successes=successes,
                trials=trials,
                unavailable_reason=reason,
                mutating_allowed=mutating_allowed.get(runtime_id, True),
            )
        )
    return out


def resolve_and_record_routing(
    *,
    workspace: str,
    store: object,
    session_id: str,
    pin: str | None = None,
    budget_usd: float | None = None,
    mutating: bool = True,
    required_capabilities: tuple[str, ...] = (),
    enabled: bool | None = None,
    costs: Mapping[str, float | None] | None = None,
    history: Mapping[str, tuple[int, int]] | None = None,
    mutating_allowed: Mapping[str, bool] | None = None,
) -> RoutingDecision | None:
    """Product path: discover → route → persist. Returns None when routing is off.

    When ``enabled`` is None, trusted global ``routing.enabled`` is read. Disabled
    registry ids never enter the candidate pool. The decision is written before
    the caller starts the runtime.
    """
    from garuda.acp.catalog import discover
    from garuda.config.agent_home import resolve_agent_home
    from garuda.interfaces.runtime_cli import configured_registry

    if enabled is None:
        home = resolve_agent_home(workspace)
        routing_cfg = (getattr(home, "global_settings", None) or {}).get("routing") or {}
        enabled = (
            bool(routing_cfg.get("enabled", False)) if isinstance(routing_cfg, dict) else False
        )
        if budget_usd is None and isinstance(routing_cfg, dict):
            raw_budget = routing_cfg.get("budget_usd")
            if raw_budget is not None:
                budget_usd = float(raw_budget)
    if not enabled:
        return None

    registry = configured_registry(workspace)
    discovered = discover(registry.manifests, disabled=registry.disabled_ids)
    candidates = candidates_from_discovered(
        discovered,
        costs=costs,
        history=history,
        mutating_allowed=mutating_allowed,
    )
    decision = route(
        candidates,
        RoutingRequest(
            required_capabilities=required_capabilities,
            pin=pin,
            budget_usd=budget_usd,
            mutating=mutating,
        ),
    )
    record_routing_decision(store, session_id, decision)
    return decision
