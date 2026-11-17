"""Accounting for model calls the agent loop does not make itself (plan task E.1, #168).

The loop records each of its own calls as a ``model_response`` event. Several other
places call ``model.complete`` directly — the summarizer, ``buffer_query``, the contract
extractor, the completion verifier and the rigorous planner/critic — and their tokens were
invisible. :func:`complete` makes such a call and records it as a ``model_response`` event
tagged with its ``call_purpose``, on the event store of the run that is executing, so
``aggregate_model_metrics`` and the usage ledger both see it, once.
Failed calls use ``model_call_failed`` so response consumers ignore them.

The run's store is found through a context variable bound by the loop for the duration of
a run; outside a run (a unit test calling a helper directly) nothing is recorded and the
call behaves exactly as before.
"""

from __future__ import annotations

import time
from contextvars import ContextVar
from typing import Any

from garuda.core.events import EventStore, EventType
from garuda.model.protocol import count_request_tokens, estimate_tools_tokens, output_token_bounds
from garuda.model.usage import (
    is_definite_rejection,
    reported_prompt_tokens,
    unreported_attempts,
    usage_metadata,
)

_events: ContextVar[EventStore | None] = ContextVar("garuda_accounting_events", default=None)
_gate: ContextVar[Any] = ContextVar("garuda_accounting_spend_gate", default=None)
_thinking_budget: ContextVar[int | None] = ContextVar("garuda_accounting_thinking_budget", default=None)

#: Output tokens reserved for an auxiliary call that does not state its own ceiling.
AUXILIARY_OUTPUT_ESTIMATE = 4096


def bind(events: EventStore, gate: Any = None, *, thinking_budget_tokens: int | None = None):
    """Make ``events`` the store auxiliary calls record to, and ``gate`` the run
    tree's spend gate they admit against; returns the reset token."""
    return _events.set(events), _gate.set(gate), _thinking_budget.set(thinking_budget_tokens)


def reset(token) -> None:
    events_token, gate_token, thinking_token = token
    _thinking_budget.reset(thinking_token)
    _gate.reset(gate_token)
    _events.reset(events_token)


def estimate_tokens(messages: list, max_tokens: int | None) -> int:
    """A deliberately rough reservation: ~4 characters per prompt token plus the
    output ceiling. Settlement charges what the provider actually reports."""
    prompt = sum(len(str(getattr(m, "content", m))) for m in messages or []) // 4
    return prompt + (max_tokens or AUXILIARY_OUTPUT_ESTIMATE)


def current() -> EventStore | None:
    return _events.get()


def current_gate() -> Any:
    """The active run tree's gate, for child runs that bind their own event store."""
    return _gate.get()


def record(purpose: str, model: Any, response: Any, duration_ms: float, *, failed: bool = False) -> None:
    events = _events.get()
    if events is None:
        return
    usage = getattr(response, "usage", None)
    events.append(EventType.MODEL_CALL_FAILED if failed else EventType.MODEL_RESPONSE, {
        "call_purpose": purpose,
        "usage": dict(usage) if isinstance(usage, dict) else {},
        "duration_ms": round(duration_ms, 3),
        "model": getattr(model, "model_name", None),
        "accounting": "auxiliary",
        **usage_metadata(response, failed=failed),
        **({"outcome": "failed"} if failed else {}),
    })


async def complete(model: Any, messages: list, *args: Any, purpose: str, **kwargs: Any):
    """``await model.complete(...)``, recorded as a ``purpose`` call, including known
    usage and unknown coverage when it fails.

    Under a run-tree spend gate the call is admitted first (a refusal raises
    :class:`~garuda.core.spend.SpendLimitExceeded`, which every auxiliary caller
    already treats as a failed call) and settled afterwards, failed or not.
    """
    gate = _gate.get()
    reservation = None
    if gate is not None:
        if gate.max_tokens is not None:
            # Normalize the Model protocol's positional options before replacing
            # max_tokens; otherwise a positional ceiling and our keyword collide.
            if len(args) > 3:
                raise TypeError("complete accepts at most tools, temperature and max_tokens")
            for name, value in zip(("tools", "temperature", "max_tokens"), args, strict=False):
                if name in kwargs:
                    raise TypeError(f"complete got multiple values for {name}")
                kwargs[name] = value
            args = ()
            requested = kwargs.get("max_tokens")
            reservation = await admit_call(
                gate, purpose, model, messages,
                tools=kwargs.get("tools"),
                max_tokens=AUXILIARY_OUTPUT_ESTIMATE if requested is None else requested,
            )
            kwargs["max_tokens"] = reservation.output_tokens
        else:
            reservation = await admit(gate, purpose, estimate_tokens(messages, kwargs.get("max_tokens")))
    started = time.monotonic()
    try:
        response = await model.complete(messages, *args, **kwargs)
    except BaseException as exc:
        if isinstance(exc, Exception):
            record(purpose, model, exc, (time.monotonic() - started) * 1000, failed=True)
        if reservation is not None:
            await settle(gate, reservation, error=exc, model=model)
        raise
    if reservation is not None:
        await settle(gate, reservation, response=response, model=model)
    record(purpose, model, response, (time.monotonic() - started) * 1000)
    return response


async def admit_call(gate: Any, purpose: str, model: Any, messages: list, *,
                     tools: list | None = None, max_tokens: int, shrink_output: bool = False):
    """Reserve the complete prompt and the exact bounded output sent to a model."""
    if callable(getattr(model, "count_tokens", None)):
        prompt = count_request_tokens(model, messages, tools)
    else:
        prompt = estimate_tokens(messages, 1) - 1 + estimate_tools_tokens(tools)
    ceiling, provider_minimum = output_token_bounds(
        model, max_tokens, thinking_budget_tokens=_thinking_budget.get())
    # Auxiliary work needs its whole requested response (e.g. a state summary).
    # Main work may use the remainder, with useful answer room after thinking.
    minimum = (min(ceiling, 1024 + (provider_minimum if provider_minimum > 1 else 0))
               if shrink_output else ceiling)
    return await admit(gate, purpose, prompt + ceiling,
                       output_tokens=ceiling, minimum_output_tokens=minimum)


async def admit(gate: Any, purpose: str, estimate: int, **bounds: Any):
    """Admit one call against ``gate``, recording a refusal on the run's store."""
    from garuda.core.spend import SpendLimitExceeded

    try:
        return await gate.admit(purpose, estimate_tokens=estimate, **bounds)
    except SpendLimitExceeded as exc:
        events = _events.get()
        if events is not None:
            events.append(EventType.BUDGET, {"spend": gate.snapshot(), "refused": purpose,
                                             "reason": str(exc)})
        raise


async def settle(gate: Any, reservation: Any, *, response: Any = None,
                 error: BaseException | None = None, model: Any = None) -> None:
    """Settle one call from its ``response`` or the ``error`` it raised; the first
    crossing of the warning threshold is recorded.

    A raised call charges whatever usage the exception carries. Without usage,
    only definite rejection of every failed attempt releases the reservation without a charge;
    interruptions and unknown outcomes charge the estimate with unknown cost.
    Otherwise, tokens are summed per attempt. Unreported attempts except definite rejections
    add the largest reported prompt size, or the reservation when unavailable,
    with unknown cost. This lower bound excludes unreported generated output.
    """
    from garuda.core.spend import usage_tokens

    source = error if error is not None else response
    interrupted = error is not None and not isinstance(error, Exception)
    usage = getattr(source, "usage", None)
    attempts = getattr(source, "attempt_usage", None) or []
    attempt_tokens = sum(
        usage_tokens(attempt["usage"]) for attempt in attempts
        if isinstance(attempt, dict) and isinstance(attempt.get("usage"), dict))
    missing = unreported_attempts(attempts)
    per_attempt = reported_prompt_tokens(attempts) or reservation.tokens
    # The final error cannot rule out spend on an earlier failed attempt.
    # Legacy attempts without rejection evidence remain unknown outcomes.
    rejected = is_definite_rejection(error) and all(
        isinstance(attempt, dict) and (
            attempt.get("outcome") != "failed" or attempt.get("rejected") is True)
        for attempt in attempts[:-1])
    # An attempt that used tokens without a price leaves the call's cost unknown,
    # whatever the other attempts reported.
    cost_partial = any(
        isinstance(attempt, dict) and isinstance(attempt.get("usage"), dict)
        and usage_tokens(attempt["usage"])
        and not isinstance(attempt["usage"].get("cost_usd"), (int, float))
        for attempt in attempts)
    warning = await gate.settle(reservation, dict(usage) if isinstance(usage, dict) else None,
                                failed=error is not None,
                                rejected=rejected, interrupted=interrupted,
                                model=getattr(model, "model_name", None),
                                cost_partial=cost_partial, attempt_tokens=attempt_tokens,
                                estimated_attempts=missing,
                                estimated_attempt_tokens=missing * per_attempt)
    events = _events.get()
    if warning is not None and events is not None:
        events.append(EventType.BUDGET, {"spend": warning})


class _Tagged:
    def __init__(self, model: Any, purpose: str):
        self._model, self._purpose = model, purpose

    async def complete(self, messages: list, *args: Any, **kwargs: Any):
        return await complete(self._model, messages, *args, purpose=self._purpose, **kwargs)

    def output_token_bounds(self, ceiling: int) -> tuple[int, int]:
        return output_token_bounds(self._model, ceiling, thinking_budget_tokens=_thinking_budget.get())


def tagged(model: Any, purpose: str) -> _Tagged:
    """``model`` for one auxiliary call site: ``await tagged(model, "summarizer").complete(...)``."""
    return _Tagged(model, purpose)
