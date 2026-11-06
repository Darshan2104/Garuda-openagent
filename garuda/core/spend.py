"""A root-scoped spend gate shared by a whole native run tree (#363, #405).

Turn, deadline and context limits bound one run's shape, not what it costs. The
gate is created once at the root (``--max-run-tokens`` / ``--max-run-cost-usd``)
and handed by reference to native subagents, so the main loop, every auxiliary
call (summarizer, buffer_query, contract, verifier, critic) and every child admit
model calls against the same remaining budget.

**Tokens are reserved.** A call reserves its estimated tokens (prompt estimate
plus the requested output ceiling) under a lock before it is sent, so concurrent
calls cannot both spend the same remainder. Settlement releases the reservation
and charges the provider-reported usage.

**Dollars are a stop, not a prepaid maximum.** A call's price is only known after
it returns, so the dollar cap refuses admission once settled spend has reached
it; a call already in flight can overshoot. Unknown cost is never counted as
zero: under a dollar cap without a token cap, the first call whose cost the
provider did not report stops further admission, because the cap can no longer
be verified.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any


class SpendLimitExceeded(RuntimeError):
    """The run tree's spend gate refused a model call."""


@dataclass(frozen=True)
class SpendReservation:
    purpose: str
    tokens: int


def usage_tokens(usage: dict | None) -> int:
    """Tokens a call consumed, from whichever fields the provider reported."""
    if not isinstance(usage, dict):
        return 0
    total = usage.get("total_tokens")
    if isinstance(total, (int, float)) and total > 0:
        return int(total)
    return sum(int(usage.get(key) or 0) for key in ("prompt_tokens", "completion_tokens"))


@dataclass
class SpendGate:
    max_tokens: int | None = None
    max_cost_usd: float | None = None
    warn_fraction: float = 0.8
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)
    reserved_tokens: int = 0
    settled_tokens: int = 0
    settled_cost_usd: float = 0.0
    cost_unknown_calls: int = 0
    calls: int = 0
    refused: int = 0
    warned: bool = False

    def __post_init__(self) -> None:
        if self.max_tokens is not None and self.max_tokens < 1:
            raise ValueError("max run tokens must be at least 1")
        if self.max_cost_usd is not None and self.max_cost_usd <= 0:
            raise ValueError("max run cost must be greater than 0")

    @property
    def limited(self) -> bool:
        return self.max_tokens is not None or self.max_cost_usd is not None

    async def admit(self, purpose: str, *, estimate_tokens: int) -> SpendReservation:
        """Reserve ``estimate_tokens`` for one call, or raise :class:`SpendLimitExceeded`."""
        estimate = max(0, int(estimate_tokens))
        async with self._lock:
            reason = self._refusal(estimate)
            if reason is not None:
                self.refused += 1
                raise SpendLimitExceeded(f"{purpose} call refused: {reason}")
            self.reserved_tokens += estimate
            self.calls += 1
            return SpendReservation(purpose, estimate)

    def _refusal(self, estimate: int) -> str | None:
        if self.max_tokens is not None:
            committed = self.settled_tokens + self.reserved_tokens
            if committed + estimate > self.max_tokens:
                return (f"run-tree token budget {self.max_tokens} would be exceeded "
                        f"({self.settled_tokens} settled, {self.reserved_tokens} reserved, "
                        f"{estimate} requested)")
        if self.max_cost_usd is not None:
            if self.settled_cost_usd >= self.max_cost_usd:
                return (f"run-tree dollar budget ${self.max_cost_usd:g} reached "
                        f"(${self.settled_cost_usd:.6f} settled)")
            if self.cost_unknown_calls and self.max_tokens is None:
                return (f"run-tree dollar budget ${self.max_cost_usd:g} cannot be verified: "
                        f"{self.cost_unknown_calls} call(s) had no reported cost")
        return None

    async def settle(self, reservation: SpendReservation, usage: dict | None) -> dict | None:
        """Release ``reservation`` and charge reported usage; returns a warning once."""
        tokens = usage_tokens(usage)
        cost = usage.get("cost_usd") if isinstance(usage, dict) else None
        async with self._lock:
            self.reserved_tokens = max(0, self.reserved_tokens - reservation.tokens)
            self.settled_tokens += tokens
            if isinstance(cost, (int, float)):
                self.settled_cost_usd = round(self.settled_cost_usd + float(cost), 8)
            else:
                # No reported price, or no usage report at all (a failed call):
                # spend happened or may have, and its price is unknown — never zero.
                self.cost_unknown_calls += 1
            if not self.warned and self._fraction() >= self.warn_fraction:
                self.warned = True
                return {**self.snapshot(), "warning": "run-tree spend passed "
                        f"{int(self.warn_fraction * 100)}% of its budget"}
        return None

    def inherit(self, snapshot: Any) -> dict[str, Any]:
        """Start from a resumed source session's recorded tree spend (#407).

        A resumed run continues the same work, so its caps cover what the source
        already spent. Without a recorded snapshot the prior spend is unknown:
        it counts as one unknown-cost call (so a dollar-only cap cannot be
        verified), and prior tokens are reported as not inherited.
        """
        fields = ("settled_tokens", "settled_cost_usd", "cost_unknown_calls")
        if not isinstance(snapshot, dict) or not all(
                isinstance(snapshot.get(k), (int, float)) and not isinstance(snapshot.get(k), bool)
                and snapshot[k] >= 0 for k in fields):
            self.cost_unknown_calls += 1
            return {"source": "unavailable", "inherited_tokens": None, "inherited_cost_usd": None,
                    "reason": "the resumed session recorded no run-tree spend; prior spend is unknown"}
        self.settled_tokens += int(snapshot["settled_tokens"])
        self.settled_cost_usd = round(self.settled_cost_usd + float(snapshot["settled_cost_usd"]), 8)
        self.cost_unknown_calls += int(snapshot["cost_unknown_calls"])
        return {"source": "spend_gate", "inherited_tokens": int(snapshot["settled_tokens"]),
                "inherited_cost_usd": round(float(snapshot["settled_cost_usd"]), 8),
                "inherited_cost_unknown_calls": int(snapshot["cost_unknown_calls"])}

    def _fraction(self) -> float:
        parts = []
        if self.max_tokens is not None:
            parts.append(self.settled_tokens / self.max_tokens)
        if self.max_cost_usd is not None:
            parts.append(self.settled_cost_usd / self.max_cost_usd)
        return max(parts, default=0.0)

    def snapshot(self) -> dict[str, Any]:
        return {
            "max_tokens": self.max_tokens,
            "max_cost_usd": self.max_cost_usd,
            "settled_tokens": self.settled_tokens,
            "reserved_tokens": self.reserved_tokens,
            "settled_cost_usd": round(self.settled_cost_usd, 8),
            "cost_unknown_calls": self.cost_unknown_calls,
            "calls": self.calls,
            "refused": self.refused,
        }
