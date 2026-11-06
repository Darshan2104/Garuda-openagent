"""Accounting for model calls the agent loop does not make itself (plan task E.1, #168).

The loop records each of its own calls as a ``model_response`` event. Several other
places call ``model.complete`` directly — the summarizer, ``buffer_query``, the contract
extractor, the completion verifier and the rigorous planner/critic — and their tokens were
invisible. :func:`complete` makes such a call and records it as a ``model_response`` event
tagged with its ``call_purpose``, on the event store of the run that is executing, so
``aggregate_model_metrics`` and the usage ledger both see it, once.

The run's store is found through a context variable bound by the loop for the duration of
a run; outside a run (a unit test calling a helper directly) nothing is recorded and the
call behaves exactly as before.
"""

from __future__ import annotations

import time
from contextvars import ContextVar
from typing import Any

from garuda.core.events import EventStore, EventType

_events: ContextVar[EventStore | None] = ContextVar("garuda_accounting_events", default=None)
_gate: ContextVar[Any] = ContextVar("garuda_accounting_spend_gate", default=None)

#: Output tokens reserved for an auxiliary call that does not state its own ceiling.
AUXILIARY_OUTPUT_ESTIMATE = 4096


def bind(events: EventStore, gate: Any = None):
    """Make ``events`` the store auxiliary calls record to, and ``gate`` the run
    tree's spend gate they admit against; returns the reset token."""
    return _events.set(events), _gate.set(gate)


def reset(token) -> None:
    events_token, gate_token = token
    _gate.reset(gate_token)
    _events.reset(events_token)


def estimate_tokens(messages: list, max_tokens: int | None) -> int:
    """A deliberately rough reservation: ~4 characters per prompt token plus the
    output ceiling. Settlement charges what the provider actually reports."""
    prompt = sum(len(str(getattr(m, "content", m))) for m in messages or []) // 4
    return prompt + (max_tokens or AUXILIARY_OUTPUT_ESTIMATE)


def current() -> EventStore | None:
    return _events.get()


def record(purpose: str, model: Any, response: Any, duration_ms: float) -> None:
    events = _events.get()
    if events is None:
        return
    usage = getattr(response, "usage", None)
    events.append(EventType.MODEL_RESPONSE, {
        "call_purpose": purpose,
        "usage": dict(usage) if isinstance(usage, dict) else {},
        "duration_ms": round(duration_ms, 3),
        "model": getattr(model, "model_name", None),
        "accounting": "auxiliary",
    })


async def complete(model: Any, messages: list, *args: Any, purpose: str, **kwargs: Any):
    """``await model.complete(...)``, recorded as a ``purpose`` call. A failed call is
    not recorded: it produced no usage to count.

    Under a run-tree spend gate the call is admitted first (a refusal raises
    :class:`~garuda.core.spend.SpendLimitExceeded`, which every auxiliary caller
    already treats as a failed call) and settled afterwards, failed or not.
    """
    gate = _gate.get()
    reservation = None
    if gate is not None:
        reservation = await admit(gate, purpose, estimate_tokens(messages, kwargs.get("max_tokens")))
    started = time.monotonic()
    response = None
    try:
        response = await model.complete(messages, *args, **kwargs)
    finally:
        if reservation is not None:
            usage = getattr(response, "usage", None) if response is not None else None
            await settle(gate, reservation, usage)
    record(purpose, model, response, (time.monotonic() - started) * 1000)
    return response


async def admit(gate: Any, purpose: str, estimate: int):
    """Admit one call against ``gate``, recording a refusal on the run's store."""
    from garuda.core.spend import SpendLimitExceeded

    try:
        return await gate.admit(purpose, estimate_tokens=estimate)
    except SpendLimitExceeded as exc:
        events = _events.get()
        if events is not None:
            events.append(EventType.BUDGET, {"spend": gate.snapshot(), "refused": purpose,
                                             "reason": str(exc)})
        raise


async def settle(gate: Any, reservation: Any, usage: Any) -> None:
    """Settle one call; the first crossing of the warning threshold is recorded."""
    warning = await gate.settle(reservation, dict(usage) if isinstance(usage, dict) else None)
    events = _events.get()
    if warning is not None and events is not None:
        events.append(EventType.BUDGET, {"spend": warning})


class _Tagged:
    def __init__(self, model: Any, purpose: str):
        self._model, self._purpose = model, purpose

    async def complete(self, messages: list, *args: Any, **kwargs: Any):
        return await complete(self._model, messages, *args, purpose=self._purpose, **kwargs)


def tagged(model: Any, purpose: str) -> _Tagged:
    """``model`` for one auxiliary call site: ``await tagged(model, "summarizer").complete(...)``."""
    return _Tagged(model, purpose)
