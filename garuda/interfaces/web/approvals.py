"""Parked tool approvals: the only thread-and-loop code in the dashboard.

``PermissionEngine`` asks for approval by awaiting a handler on the agent's loop. A browser
answers on an HTTP thread. So a pending approval is an ``asyncio.Future`` created on the
loop and resolved from somewhere else, which is a narrow enough contract to deserve its own
module and its own test file.

Four decisions carry it, each fixing a failure that is otherwise silent:

**Silence is denial.** ``asyncio.wait_for`` with a timeout, returning ``False``. A tab
closed mid-ask must not wedge the run — and its container, its MCP subprocess and its
persistent shell — for the full timeout, so the poll doubles as a heartbeat and an owner
that has not been seen in ``HEARTBEAT_GRACE_SECONDS`` gets its pending asks denied. The
result is an ordinary denial, exactly what answering ``n`` at the CLI prompt produces, and
the agent carries on.

**Resolution happens on the loop, and its outcome comes back from there.** A future is
loop-affine, so ``set_result`` from an HTTP thread is undefined behaviour that usually looks
like it works. Checking ``done()`` on the calling thread is no better: the timeout and the
reaper can both complete the future in the window between the check and the set, and
``set_result`` on a finished future raises ``InvalidStateError`` *inside the loop's
exception handler*, where nobody sees it — while the HTTP request has already returned
success. So check-and-set is one operation executed on the loop, and its boolean result is
what becomes a 200 or a 409. The two entry points differ only in who is calling:
``resolve_from_thread`` marshals, and ``reap``/``forget`` already run on the loop and settle
directly. Marshalling from the loop's own thread would deadlock.

**Structured arguments are recovered, not parsed.** The engine hands the handler a
preformatted ``f"{tool_name}({arguments})"`` string, and ``screen()`` runs *before* the
``tool_call`` event is emitted — so at park time the newest ``model_response`` holds
``tool_calls: [{id, name, arguments}]`` built from the **same dict object** that produced
that string. Exact string equality therefore recovers the structure; it is a lookup, not a
heuristic. No match falls back to showing the raw string: degraded, never wrong.

**Cancellation cleans up by construction.** Cancelling the job raises ``CancelledError`` at
the ``await`` inside ``evaluate_tool_call``; the handler's ``finally`` unparks the ask and
``CancelledError`` propagates untouched, because ``JobManager`` needs it to mark the job
``CANCELLED`` and ``run_agent_task``'s own ``finally`` needs it to tear the workspace down.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from garuda.core.events import EventStore

logger = logging.getLogger(__name__)

#: How long an unanswered ask waits before being denied. Generous, because the heartbeat
#: below is what actually catches an abandoned tab — this is the backstop for a tab that is
#: open but unattended.
APPROVAL_TIMEOUT_SECONDS = 300.0

#: How long after an owner's last request its pending asks are denied. Must be comfortably
#: longer than the client's poll interval and shorter than a person's patience.
HEARTBEAT_GRACE_SECONDS = 30.0


@dataclass
class PendingAsk:
    """One approval waiting for an answer."""

    ask_id: str
    owner: str
    action: str
    created_at: float
    future: asyncio.Future = field(repr=False)
    tool_name: str | None = None
    tool_call_id: str | None = None
    arguments: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "ask_id": self.ask_id,
            "owner": self.owner,
            # The raw string always travels, even when the structure was recovered: it is
            # what the permission engine actually screened, and the two disagreeing would
            # be worth seeing.
            "action": self.action,
            "tool_name": self.tool_name,
            "tool_call_id": self.tool_call_id,
            "arguments": self.arguments,
            "waiting_ms": round((time.monotonic() - self.created_at) * 1000),
        }


class ApprovalBroker:
    """Parks asks, answers them, and denies the ones nobody came back for."""

    def __init__(
        self,
        *,
        timeout: float = APPROVAL_TIMEOUT_SECONDS,
        grace: float = HEARTBEAT_GRACE_SECONDS,
    ) -> None:
        self._pending: dict[str, PendingAsk] = {}
        self._seen: dict[str, float] = {}
        self._timeout = timeout
        self._grace = grace

    @property
    def timeout_seconds(self) -> float:
        return self._timeout

    @property
    def grace_seconds(self) -> float:
        """The client polls faster than this, so it is published rather than duplicated as a
        constant on both sides."""
        return self._grace

    # -- the handler the permission engine awaits ----------------------------

    def make_handler(self, owner: str, events: "EventStore | Callable[[], EventStore | None]"):
        """An ``ApprovalHandler`` bound to one chat or run.

        ``events`` may be a store or a zero-argument callable returning one. The callable
        form exists because ``AgentSession.create`` takes the handler as an argument and
        creates the store itself — so at handler-construction time the store does not exist
        yet. The alternative was assigning ``permissions._approval_handler`` afterwards,
        which reaches into a private field to work around an ordering problem.
        """

        async def handler(action: str) -> bool:
            ask = self._park(owner, action, events)
            try:
                # No `shield`: `wait_for` cancelling the future on timeout is exactly the
                # behaviour wanted. A cancelled future is `done()`, so a late answer from
                # the browser settles nothing instead of resolving something unreachable.
                return await asyncio.wait_for(ask.future, self._timeout)
            except asyncio.TimeoutError:
                logger.info("Approval %s timed out; denying", ask.ask_id)
                return False
            finally:
                # Also the cancellation path: an unparked ask cannot be answered by a
                # browser that no longer has anything to answer.
                self._pending.pop(ask.ask_id, None)

        return handler

    def _park(self, owner: str, action: str, events) -> PendingAsk:
        # Created on the loop the agent runs on, which is the loop that will await it.
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        ask = PendingAsk(
            ask_id=uuid.uuid4().hex,
            owner=owner,
            action=action,
            created_at=time.monotonic(),
            future=future,
        )
        self._attach_structure(ask, events)
        self._pending[ask.ask_id] = ask
        self.touch(owner)
        logger.info("Parked approval %s for %s: %s", ask.ask_id, owner, action)
        return ask

    def _attach_structure(self, ask: PendingAsk, events) -> None:
        """Recover ``{id, name, arguments}`` from the newest ``model_response``.

        Exact string equality against ``f"{name}({arguments})"``, because that is precisely
        how the engine built the string it handed us — from the same dict object, in the
        same process, microseconds earlier. A near-match would be a heuristic; this is a
        lookup. Failure leaves the raw string in place.
        """
        store = events() if callable(events) else events
        if store is None:
            return
        try:
            records = store.get_all()
        except Exception:  # pragma: no cover - a store that cannot be read is not fatal
            logger.debug("Could not read events while parking an approval", exc_info=True)
            return
        for record in reversed(records):
            if record.get("type") != "model_response":
                continue
            payload = record.get("payload")
            if not isinstance(payload, dict):
                continue
            for call in payload.get("tool_calls") or []:
                if not isinstance(call, dict):
                    continue
                name, arguments = call.get("name"), call.get("arguments")
                if name is None:
                    continue
                if f"{name}({arguments})" == ask.action:
                    ask.tool_name = name
                    ask.tool_call_id = call.get("id")
                    ask.arguments = arguments if isinstance(arguments, dict) else None
                    return
            # Only the newest response can be the one being screened, so stop at the
            # first one rather than searching back through the whole conversation and
            # risking a match on an identical earlier call.
            return

    # -- answering -----------------------------------------------------------

    def resolve_from_thread(
        self, ask_id: str, approved: bool, *, loop: asyncio.AbstractEventLoop
    ) -> str:
        """Answer an ask from an HTTP thread. Returns the outcome, accurately.

        The answer comes back from *inside* the loop rather than being inferred outside it.
        A `done()` check on the calling thread would be a guess: the timeout and the reaper
        can both complete the future in the window between checking and setting, and this
        method's return value is what becomes a 200 or a 409.
        """
        ask = self._pending.get(ask_id)
        if ask is None:
            return "unknown"

        async def settle() -> bool:
            return self.settle(ask, approved)

        try:
            done = asyncio.run_coroutine_threadsafe(settle(), loop).result(timeout=5)
        except RuntimeError:
            # The loop is gone, so whatever it was awaiting is gone with it.
            return "already_resolved"
        return "resolved" if done else "already_resolved"

    def settle(self, ask: PendingAsk, approved: bool) -> bool:
        """Complete one ask. **Must be called on the agent loop** — a future is loop-affine,
        and `set_result` from another thread is undefined behaviour that usually appears to
        work. Returns False if it was already resolved."""
        if ask.future.done():
            return False
        ask.future.set_result(approved)
        return True

    def pending(self, owner: str | None = None) -> list[dict[str, Any]]:
        return [
            ask.as_dict()
            for ask in self._pending.values()
            if owner is None or ask.owner == owner
        ]

    def touch(self, owner: str) -> None:
        """Record that an owner is still watching. Called on any request naming it."""
        self._seen[owner] = time.monotonic()

    # -- the reaper ----------------------------------------------------------

    def reap(self) -> int:
        """Deny every ask whose owner has not been seen inside the grace window.

        **Runs on the agent loop**, as a periodic task — which is why it calls `settle`
        directly rather than marshalling. This, not the timeout, is what makes navigating
        away safe: a five-minute wedge holding a container and an MCP subprocess open is a
        worse outcome than a denial the user can retry.
        """
        cutoff = time.monotonic() - self._grace
        denied = 0
        for ask in list(self._pending.values()):
            if self._seen.get(ask.owner, 0.0) >= cutoff:
                continue
            logger.info("Owner %s went quiet; denying approval %s", ask.owner, ask.ask_id)
            if self.settle(ask, False):
                denied += 1
        return denied

    def forget(self, owner: str) -> int:
        """Deny everything for an owner that is going away. Runs on the agent loop."""
        self._seen.pop(owner, None)
        denied = 0
        for ask in list(self._pending.values()):
            if ask.owner == owner and self.settle(ask, False):
                denied += 1
        return denied

    def __len__(self) -> int:
        return len(self._pending)
