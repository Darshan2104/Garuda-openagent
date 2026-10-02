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


def bind(events: EventStore):
    """Make ``events`` the store auxiliary calls record to; returns the reset token."""
    return _events.set(events)


def reset(token) -> None:
    _events.reset(token)


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
    not recorded: it produced no usage to count."""
    started = time.monotonic()
    response = await model.complete(messages, *args, **kwargs)
    record(purpose, model, response, (time.monotonic() - started) * 1000)
    return response


class _Tagged:
    def __init__(self, model: Any, purpose: str):
        self._model, self._purpose = model, purpose

    async def complete(self, messages: list, *args: Any, **kwargs: Any):
        return await complete(self._model, messages, *args, purpose=self._purpose, **kwargs)


def tagged(model: Any, purpose: str) -> _Tagged:
    """``model`` for one auxiliary call site: ``await tagged(model, "summarizer").complete(...)``."""
    return _Tagged(model, purpose)
