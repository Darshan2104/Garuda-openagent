"""A root-scoped spend gate shared by a whole native run tree (#363, #405).

Turn, deadline and context limits bound one run's shape, not what it costs. The
gate is created once at the root (``--max-run-tokens`` / ``--max-run-cost-usd``)
and handed by reference to native subagents, so the main loop, every auxiliary
call (summarizer, buffer_query, contract, verifier, critic) and every child admit
model calls against the same remaining budget.

**Tokens are reserved.** A call reserves its estimated tokens (prompt estimate
plus the requested output ceiling) under a lock before it is sent, so concurrent
calls cannot both spend the same remainder. Settlement releases the reservation
and charges provider-reported usage or the estimate when usage is unknown. The
root checkpoints admission before sending and settlement afterwards. A failed
admission checkpoint refuses the call; a failed settlement checkpoint leaves the
recorded reservation intact.
On crash recovery, outstanding reservations are charged at their token estimate
with unknown cost, and interrupted-call provenance survives later resumes.

**Dollars are a stop, not a prepaid maximum.** A call's price is only known after
it returns, so the dollar cap refuses admission once settled spend has reached
it; a call already in flight can overshoot. Unknown cost is never counted as
zero: under a dollar cap without a token cap, the first completed, interrupted or
unknown-outcome call whose cost the provider did not report stops further
admission, because the cap cannot be enforced. Pricing is only learned from a
response, so this cannot be refused before the first call.

**Unreported failures fail closed.** A definite rejection (context overflow or
HTTP 4xx except 408) releases its reservation and counts as failed without usage.
An interrupted call or any other failure with no reported usage is charged its
reservation estimate with unknown cost, retaining interrupted or unknown-outcome
provenance. Calls that were never sent cannot reliably be distinguished, so this
can overcharge. Usage the exception carries is charged like any other report.
For interruptions, reported usage is a lower bound: charge at least the
reservation estimate, retain known dollars and always count the cost as unknown.
A trailing interrupted active attempt is charged at least its own reservation
on top of known earlier attempts; any increase over its reported tokens is estimated.
For calls with reported usage or a successful completion, tokens are summed per
attempt. Each attempt without usage, except a definite failed rejection, adds the
largest reported prompt size in that call, or its reservation estimate if unavailable,
with unknown cost. This lower bound excludes unreported generated output.
The whole-call failure estimate above is never charged these extras too.

**A completed call with no token fields is charged its estimate.** A provider
that returns no usage at all (a stream whose server ignored ``include_usage``)
did consume tokens; settling it as zero would leave the token cap unenforced, so
its reservation estimate is charged instead and counted as ``estimated_token_calls``.
"""

from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# Older checkpoints must contain the core totals; later counters may be absent.
SPEND_REQUIRED_FIELDS = ("settled_tokens", "settled_cost_usd", "cost_unknown_calls")
SPEND_OPTIONAL_COUNTERS = ("reserved_tokens", "reserved_calls", "interrupted_calls",
                           "unknown_outcome_calls", "estimated_token_calls", "unreported_failed_attempts")


class SpendLimitExceeded(RuntimeError):
    """The run tree's spend gate refused a model call.

    ``cause`` says which limit refused it: ``tokens``, ``dollars`` (reported spend
    reached the cap) or ``cost_unknown`` (the dollar cap cannot be enforced because
    a completed, interrupted or unknown-outcome call has unknown cost), or
    ``checkpoint`` (the reservation could not be persisted before sending).
    """

    def __init__(self, message: str, cause: str):
        super().__init__(message)
        self.cause = cause


@dataclass(frozen=True)
class SpendReservation:
    purpose: str
    tokens: int
    output_tokens: int | None = None


def usage_tokens(usage: dict | None) -> int:
    """Tokens a call consumed, from whichever fields the provider reported."""
    if not isinstance(usage, dict):
        return 0
    total = usage.get("total_tokens")
    if isinstance(total, (int, float)) and total > 0:
        return int(total)
    return sum(int(usage.get(key) or 0) for key in ("prompt_tokens", "completion_tokens"))


def reports_tokens(usage: dict | None) -> bool:
    """Whether the provider reported any token count, zero included."""
    return isinstance(usage, dict) and any(
        isinstance(usage.get(key), (int, float))
        for key in ("total_tokens", "prompt_tokens", "completion_tokens"))


def gate_for(config: Any) -> SpendGate | None:
    """The run tree's gate on ``config``, created once when either cap is set.

    Every phase that can start a run tree (``prepare_run``, the rigorous plan and
    critic) goes through here, so a later phase reuses the gate an earlier one made.
    """
    if config.spend_gate is None and (config.max_run_tokens is not None
                                      or config.max_run_cost_usd is not None):
        config.spend_gate = SpendGate(max_tokens=config.max_run_tokens,
                                      max_cost_usd=config.max_run_cost_usd)
    return config.spend_gate


@dataclass
class SpendGate:
    max_tokens: int | None = None
    max_cost_usd: float | None = None
    warn_fraction: float = 0.8
    checkpoint: Callable[[dict], None] | None = field(default=None, repr=False, compare=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)
    reserved_tokens: int = 0
    reserved_calls: int = 0
    interrupted_calls: int = 0
    unknown_outcome_calls: int = 0
    settled_tokens: int = 0
    settled_cost_usd: float = 0.0
    cost_unknown_calls: int = 0
    unpriced_model: str | None = None
    prior_spend_unknown: bool = False
    failed_without_usage: int = 0
    estimated_token_calls: int = 0
    # Retain the checkpoint key; it also counts successful attempts without usage.
    unreported_failed_attempts: int = 0
    calls: int = 0
    refused: int = 0
    warned: bool = False
    _last_refusal_cause: str | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        # A cap that compares false against everything (NaN) or can never be
        # reached (inf) would silently disable itself: refused, not accepted.
        if self.max_tokens is not None and self.max_tokens < 1:
            raise ValueError("--max-run-tokens must be at least 1")
        if self.max_cost_usd is not None and not (
                math.isfinite(self.max_cost_usd) and self.max_cost_usd > 0):
            raise ValueError("--max-run-cost-usd must be a finite amount greater than 0")

    @property
    def limited(self) -> bool:
        return self.max_tokens is not None or self.max_cost_usd is not None

    @property
    def last_refusal_cause(self) -> str | None:
        """Most recent admission refusal's cause, or ``None`` before any refusal.

        Phase coordinators pair this with ``refused`` to retain the spend cause
        when a worker's auxiliary caller catches the admission exception.
        """
        return self._last_refusal_cause

    def would_refuse(self, estimate_tokens: int) -> str | None:
        """Read-only advisory admission check; zero also detects an exhausted cap.

        A token cap is exhausted at settled + reserved >= max, so this refuses
        at equality even when ``admit(estimate_tokens=0)`` could reserve zero.
        Per-call ``admit`` remains authoritative and owns reservation accounting.
        """
        estimate = max(0, int(estimate_tokens))
        if (self.max_tokens is not None
                and self.settled_tokens + self.reserved_tokens >= self.max_tokens):
            return "tokens"
        refusal = self._refusal(estimate)
        return refusal[0] if refusal is not None else None

    async def admit(self, purpose: str, *, estimate_tokens: int,
                    output_tokens: int | None = None,
                    minimum_output_tokens: int = 1) -> SpendReservation:
        """Reserve a call, or raise :class:`SpendLimitExceeded`.

        With ``output_tokens``, the estimate includes that ceiling. Under a token
        cap, shrink it atomically to the remainder and return the exact ceiling
        the caller must send. Refuse when even ``minimum_output_tokens`` cannot fit.
        """
        estimate = max(0, int(estimate_tokens))
        if output_tokens is not None and (
                type(output_tokens) is not int or type(minimum_output_tokens) is not int
                or minimum_output_tokens < 1 or output_tokens < minimum_output_tokens):
            raise ValueError("model output ceiling must be a positive integer at least its minimum")
        async with self._lock:
            if self.max_tokens is not None and output_tokens is not None:
                # Choose and reserve under the same lock: concurrent calls cannot
                # both send a ceiling derived from the same remaining tokens.
                prompt = max(0, estimate - output_tokens)
                remaining = self.max_tokens - self.settled_tokens - self.reserved_tokens - prompt
                output_tokens = min(output_tokens, remaining)
                estimate = prompt + max(output_tokens, minimum_output_tokens)
            refusal = self._refusal(estimate)
            if refusal is not None:
                self.refused += 1
                cause, reason = refusal
                self._last_refusal_cause = cause
                raise SpendLimitExceeded(f"{purpose} call refused: {reason}", cause)
            self.reserved_tokens += estimate
            self.reserved_calls += 1
            self.calls += 1
            if self.checkpoint is not None:
                try:
                    self.checkpoint(self.snapshot())
                except BaseException as exc:
                    self.reserved_tokens -= estimate
                    self.reserved_calls -= 1
                    self.calls -= 1
                    if not isinstance(exc, Exception):
                        raise
                    self.refused += 1
                    self._last_refusal_cause = "checkpoint"
                    raise SpendLimitExceeded(
                        f"{purpose} call refused: spend checkpoint could not be recorded",
                        "checkpoint") from exc
            return SpendReservation(purpose, estimate, output_tokens)

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
                if (self.interrupted_calls or self.unknown_outcome_calls
                        or self.unreported_failed_attempts) and not self.unpriced_model:
                    clauses = []
                    if self.interrupted_calls:
                        clauses.append(f"{self.interrupted_calls} interrupted call(s) have unknown cost")
                    if self.unknown_outcome_calls:
                        clauses.append(f"{self.unknown_outcome_calls} unknown-outcome call(s) have unknown cost")
                    if self.unreported_failed_attempts:
                        clauses.append(f"{self.unreported_failed_attempts} attempt(s) without reported usage")
                    return "cost_unknown", (
                        f"run-tree dollar budget ${self.max_cost_usd:g} cannot be enforced: "
                        f"{'; '.join(clauses)}; "
                        "use --max-run-tokens to bound this run")
                if self.prior_spend_unknown and not self.unpriced_model:
                    return "cost_unknown", (
                        f"run-tree dollar budget ${self.max_cost_usd:g} cannot be enforced: "
                        "a resumed session in this chain recorded no run-tree spend, so prior "
                        "spend is unknown; use --max-run-tokens to bound this run")
                model = f" ({self.unpriced_model})" if self.unpriced_model else ""
                return "cost_unknown", (
                    f"run-tree dollar budget ${self.max_cost_usd:g} cannot be enforced for "
                    f"this model{model}: {self.cost_unknown_calls} completed call(s) "
                    "returned no provider-reported cost; use --max-run-tokens to bound "
                    "spend for a model whose pricing is unavailable")
        return None

    async def settle(self, reservation: SpendReservation, usage: dict | None, *,
                     failed: bool = False, rejected: bool = False, interrupted: bool = False,
                     model: str | None = None,
                     cost_partial: bool = False, attempt_tokens: int = 0,
                     estimated_attempts: int = 0, estimated_attempt_tokens: int = 0) -> dict | None:
        """Release ``reservation`` and charge reported usage; returns a warning once.

        ``failed`` marks a call that raised; ``usage`` is then whatever the
        exception carried, usually nothing. With no usage, only ``rejected``
        releases the reservation without charge; otherwise its estimate is
        charged with unknown cost, classified by ``interrupted``.
        ``cost_partial`` marks a call whose
        attempts (a retry, a stream fallback) used tokens that some attempt
        reported no price for: its known cost is charged and it still counts as
        unknown cost, so one priced attempt cannot make the whole call look priced.
        ``attempt_tokens`` preserves per-attempt token totals. Unreported non-rejected
        attempts add ``estimated_attempt_tokens`` and unknown cost once per call,
        except when the whole-call failure branch already charges its estimate.
        Model accounting charges an interrupted active attempt at least its own
        reservation on top of known earlier attempts. For an interrupted call with
        usage, this per-attempt total is also floored at the reservation estimate
        rather than adding another estimate. Known dollars
        are retained, but the call always has unknown cost and interrupted provenance.
        """
        tokens = usage_tokens(usage)
        cost = usage.get("cost_usd") if isinstance(usage, dict) else None
        async with self._lock:
            self.reserved_tokens = max(0, self.reserved_tokens - reservation.tokens)
            self.reserved_calls = max(0, self.reserved_calls - 1)
            if failed and not tokens and not isinstance(cost, (int, float)):
                if rejected:
                    self.failed_without_usage += 1
                else:
                    # The provider may have processed the request. Keep its
                    # estimate and unknown cost even if it was never sent.
                    self.settled_tokens += reservation.tokens
                    self.estimated_token_calls += 1
                    self.cost_unknown_calls += 1
                    if interrupted:
                        self.interrupted_calls += 1
                    else:
                        self.unknown_outcome_calls += 1
            else:
                estimated = bool(estimated_attempts)
                if attempt_tokens:
                    tokens = attempt_tokens
                elif not failed and not estimated_attempts and not reports_tokens(usage):
                    # Completed without any token count: the call consumed
                    # tokens nobody reported, so its estimate stands in.
                    tokens = reservation.tokens
                    estimated = True
                tokens += estimated_attempt_tokens
                if failed and interrupted:
                    # Partial usage cannot prove the final bill. Apply the floor
                    # after per-attempt estimates so the reservation is not added twice.
                    estimated = estimated or reservation.tokens > tokens
                    tokens = max(tokens, reservation.tokens)
                    self.interrupted_calls += 1
                if estimated:
                    self.estimated_token_calls += 1
                self.unreported_failed_attempts += estimated_attempts
                self.settled_tokens += tokens
                if isinstance(cost, (int, float)):
                    self.settled_cost_usd = round(self.settled_cost_usd + float(cost), 8)
                unpriced = not isinstance(cost, (int, float)) or cost_partial
                if unpriced or estimated_attempts or (failed and interrupted):
                    # Reported usage without a price: spend happened and its price
                    # is unknown — never zero.
                    self.cost_unknown_calls += 1
                    if unpriced:
                        self.unpriced_model = self.unpriced_model or model
            if self.checkpoint is not None:
                try:
                    self.checkpoint(self.snapshot())
                except Exception:
                    logger.warning("Spend settlement checkpoint failed; recorded reservation retained",
                                   exc_info=True)
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
        Outstanding checkpoint reservations are interrupted calls: charge their
        estimates as tokens and retain unknown cost, never a priced zero.
        """
        if not isinstance(snapshot, dict) or not all(
                isinstance(snapshot.get(k), (int, float)) and not isinstance(snapshot.get(k), bool)
                and snapshot[k] >= 0 for k in SPEND_REQUIRED_FIELDS) or not all(
                    type(snapshot.get(k, 0)) is int and snapshot.get(k, 0) >= 0
                    for k in SPEND_OPTIONAL_COUNTERS):
            self.cost_unknown_calls += 1
            self.prior_spend_unknown = True
            return {"source": "unavailable", "inherited_tokens": None, "inherited_cost_usd": None,
                    "reason": "the resumed session recorded no run-tree spend; prior spend is unknown"}
        self.settled_tokens += int(snapshot["settled_tokens"])
        self.settled_cost_usd = round(self.settled_cost_usd + float(snapshot["settled_cost_usd"]), 8)
        self.cost_unknown_calls += int(snapshot["cost_unknown_calls"])
        self.prior_spend_unknown = self.prior_spend_unknown or snapshot.get("prior_spend_unknown") is True
        self.unpriced_model = self.unpriced_model or snapshot.get("unpriced_model")
        self.interrupted_calls += snapshot.get("interrupted_calls", 0)
        self.unknown_outcome_calls += snapshot.get("unknown_outcome_calls", 0)
        self.estimated_token_calls += snapshot.get("estimated_token_calls", 0)
        self.unreported_failed_attempts += snapshot.get("unreported_failed_attempts", 0)
        inherited = {"source": "spend_gate", "inherited_tokens": int(snapshot["settled_tokens"]),
                     "inherited_cost_usd": round(float(snapshot["settled_cost_usd"]), 8),
                     "inherited_cost_unknown_calls": int(snapshot["cost_unknown_calls"])}
        interrupted = snapshot.get("reserved_calls", 0)
        if interrupted:
            reserved = snapshot.get("reserved_tokens", 0)
            self.settled_tokens += reserved
            self.estimated_token_calls += interrupted
            self.cost_unknown_calls += interrupted
            self.interrupted_calls += interrupted
            inherited.update(source="spend_checkpoint",
                             inherited_tokens=int(snapshot["settled_tokens"]) + reserved,
                             inherited_cost_unknown_calls=int(snapshot["cost_unknown_calls"]) + interrupted,
                             interrupted_calls=interrupted, interrupted_tokens_charged=reserved)
        return inherited

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
            "reserved_calls": self.reserved_calls,
            "interrupted_calls": self.interrupted_calls,
            "unknown_outcome_calls": self.unknown_outcome_calls,
            "settled_cost_usd": round(self.settled_cost_usd, 8),
            "cost_unknown_calls": self.cost_unknown_calls,
            "prior_spend_unknown": self.prior_spend_unknown,
            "unpriced_model": self.unpriced_model,
            "failed_without_usage": self.failed_without_usage,
            "estimated_token_calls": self.estimated_token_calls,
            "unreported_failed_attempts": self.unreported_failed_attempts,
            "calls": self.calls,
            "refused": self.refused,
        }
