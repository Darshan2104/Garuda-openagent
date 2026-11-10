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
zero: under a dollar cap without a token cap, the first completed call whose
cost the provider did not report stops further admission, because the cap cannot
be enforced for that model. Pricing is only learned from a response, so this
cannot be refused before the first call.

**A raised call is not an unpriced call.** A call that raised before the provider
returned any usage (a context-overflow rejection, a transport error) releases its
reservation and is counted apart as failed without usage; usage the exception
carries is charged like any other. Such a call's spend, if the provider billed
it at all, is not reported to Garuda and is not added to the settled total.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any


class SpendLimitExceeded(RuntimeError):
    """The run tree's spend gate refused a model call.

    ``cause`` says which limit refused it: ``tokens``, ``dollars`` (reported spend
    reached the cap) or ``cost_unknown`` (the dollar cap cannot be enforced because
    a completed call reported no cost).
    """

    def __init__(self, message: str, cause: str):
        super().__init__(message)
        self.cause = cause


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
    unpriced_model: str | None = None
    failed_without_usage: int = 0
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
            refusal = self._refusal(estimate)
            if refusal is not None:
                self.refused += 1
                cause, reason = refusal
                raise SpendLimitExceeded(f"{purpose} call refused: {reason}", cause)
            self.reserved_tokens += estimate
            self.calls += 1
            return SpendReservation(purpose, estimate)

    def _refusal(self, estimate: int) -> tuple[str, str] | None:
        if self.max_tokens is not None:
            committed = self.settled_tokens + self.reserved_tokens
            if committed + estimate > self.max_tokens:
                return "tokens", (f"run-tree token budget {self.max_tokens} would be exceeded "
                                  f"({self.settled_tokens} settled, {self.reserved_tokens} "
                                  f"reserved, {estimate} requested)")
        if self.max_cost_usd is not None:
            if self.settled_cost_usd >= self.max_cost_usd:
                return "dollars", (f"run-tree dollar budget ${self.max_cost_usd:g} reached "
                                   f"(${self.settled_cost_usd:.6f} settled)")
            if self.cost_unknown_calls and self.max_tokens is None:
                model = f" ({self.unpriced_model})" if self.unpriced_model else ""
                return "cost_unknown", (
                    f"run-tree dollar budget ${self.max_cost_usd:g} cannot be enforced for "
                    f"this model{model}: {self.cost_unknown_calls} completed call(s) "
                    "returned no provider-reported cost; use --max-run-tokens to bound "
                    "spend for a model whose pricing is unavailable")
        return None

    async def settle(self, reservation: SpendReservation, usage: dict | None, *,
                     failed: bool = False, model: str | None = None) -> dict | None:
        """Release ``reservation`` and charge reported usage; returns a warning once.

        ``failed`` marks a call that raised; ``usage`` is then whatever the
        exception carried, usually nothing.
        """
        tokens = usage_tokens(usage)
        cost = usage.get("cost_usd") if isinstance(usage, dict) else None
        async with self._lock:
            self.reserved_tokens = max(0, self.reserved_tokens - reservation.tokens)
            if failed and not tokens and not isinstance(cost, (int, float)):
                # Raised before the provider reported anything (an overflow
                # rejection, a transport error): counted apart, not as a priced
                # zero and not as a completed call whose price is unknown.
                self.failed_without_usage += 1
            else:
                self.settled_tokens += tokens
                if isinstance(cost, (int, float)):
                    self.settled_cost_usd = round(self.settled_cost_usd + float(cost), 8)
                else:
                    # Reported usage without a price: spend happened and its price
                    # is unknown — never zero.
                    self.cost_unknown_calls += 1
                    self.unpriced_model = self.unpriced_model or model
            if not self.warned and self._fraction() >= self.warn_fraction:
                self.warned = True
                return {**self.snapshot(), "warning": "run-tree spend passed "
                        f"{int(self.warn_fraction * 100)}% of its budget"}
        return None

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
            "failed_without_usage": self.failed_without_usage,
            "calls": self.calls,
            "refused": self.refused,
        }
