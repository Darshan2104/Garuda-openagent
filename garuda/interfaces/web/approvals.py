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
agent sees a denial and carries on, but the handler returns a falsy
:class:`~garuda.acp.broker.Unanswered` (timeout or disconnect) so the audit records what
actually happened rather than a "no" nobody gave.

**Who decided is kept, not inferred.** The permission engine turns every falsy answer into
the same "User denied" sentence, so the transcript alone cannot tell a person's "no" from the
reaper's. The broker records each settled ask — approved, denied by the person, timed out,
reaped after the heartbeat stopped, or denied because the chat closed — and
``/api/approvals`` serves that record, so the chat shows a reaper denial as exactly that.

**An answer names the ask it saw.** Each ask carries a digest of its id, owner and screened
action; an answer must name the same owner and digest, so a stale card or another chat's
listing cannot settle it.

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
import hashlib
import logging
import time
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from garuda.acp.broker import ApprovalOutcome, Unanswered
from garuda.core.events import EventStore

logger = logging.getLogger(__name__)

#: How long an unanswered ask waits before being denied. Generous, because the heartbeat
#: below is what actually catches an abandoned tab — this is the backstop for a tab that is
#: open but unattended.
APPROVAL_TIMEOUT_SECONDS = 300.0

#: How long after an owner's last request its pending asks are denied. It has to outlast the
#: slowest heartbeat a *watching* browser can send, and that is set by background-tab timer
#: throttling, not by the client's own interval: Chrome's intensive throttling lets a page
#: hidden for five minutes wake its chained timers at most once a minute, and other browsers
#: budget background timers similarly. So 60 s is the worst gap between beats from a tab
#: that is still open, and 90 s leaves 30 s for a slow request and a reaper tick. The reaper
#: runs every 10 s, so a tab that really is gone has its ask denied within about 100 s —
#: still a third of the five-minute timeout it exists to beat. (30 s, the old value, reaped
#: the approvals of people who had only switched tabs.)
HEARTBEAT_GRACE_SECONDS = 90.0

#: How many settled asks the broker remembers, across owners, for the transcript to explain.
SETTLED_HISTORY = 200

#: Who settled an ask. ``you`` is the only one that is a person's decision.
DECIDED_BY_USER = "you"
DECIDED_BY_TIMEOUT = "timeout"
DECIDED_BY_REAPER = "stopped_watching"
DECIDED_BY_CLOSE = "chat_closed"
DECIDED_BY_CANCEL = "cancelled"


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
    #: Set by whichever path completed the future; read when the handler records it.
    decided_by: str | None = None

    @property
    def digest(self) -> str:
        """Binds an answer to this ask's id, owner and the exact screened action."""
        material = "\x00".join((self.ask_id, self.owner, self.action))
        return hashlib.sha256(material.encode()).hexdigest()

    def as_dict(self) -> dict[str, Any]:
        return {
            "ask_id": self.ask_id,
            "owner": self.owner,
            "digest": self.digest,
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
        self._settled: deque[dict[str, Any]] = deque(maxlen=SETTLED_HISTORY)
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

        async def handler(action: str) -> "bool | Unanswered":
            ask = self._park(owner, action, events)
            answer: "bool | Unanswered | None" = None
            try:
                # No `shield`: `wait_for` cancelling the future on timeout is exactly the
                # behaviour wanted. A cancelled future is `done()`, so a late answer from
                # the browser settles nothing instead of resolving something unreachable.
                answer = await asyncio.wait_for(ask.future, self._timeout)
                return answer
            except asyncio.TimeoutError:
                logger.info("Approval %s timed out; denying", ask.ask_id)
                ask.decided_by = DECIDED_BY_TIMEOUT
                answer = Unanswered(ApprovalOutcome.TIMEOUT,
                                    "dashboard approval timed out without an answer")
                return answer
            finally:
                # Also the cancellation path: an unparked ask cannot be answered by a
                # browser that no longer has anything to answer.
                self._pending.pop(ask.ask_id, None)
                # Recorded before the engine emits its `permission_ask` event, so a client
                # that reads this after seeing the event always finds it.
                self._record(ask, answer)

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

    def _record(self, ask: PendingAsk, answer: "bool | Unanswered | None") -> None:
        decided_by = ask.decided_by or (DECIDED_BY_CANCEL if answer is None else DECIDED_BY_USER)
        self._settled.append({
            "ask_id": ask.ask_id,
            "owner": ask.owner,
            "action": ask.action,
            "tool_name": ask.tool_name,
            "tool_call_id": ask.tool_call_id,
            "approved": answer is True,
            "decided_by": decided_by,
            "summary": self._summary(decided_by, answer is True),
            "reason": answer.reason if isinstance(answer, Unanswered) else None,
            "settled_at": datetime.now(timezone.utc).isoformat(),
        })

    def _summary(self, decided_by: str, approved: bool) -> str:
        """One sentence a person can read, and the only wording the client shows."""
        if decided_by == DECIDED_BY_USER:
            return "approved by you" if approved else "denied by you"
        if decided_by == DECIDED_BY_TIMEOUT:
            return f"denied: nobody answered within {self._timeout:g}s"
        if decided_by == DECIDED_BY_REAPER:
            return (f"denied: the dashboard stopped watching "
                    f"(no heartbeat for {self._grace:g}s)")
        if decided_by == DECIDED_BY_CLOSE:
            return "denied: the chat was closed"
        return "withdrawn: the turn was stopped before it was answered"

    def settled(self, owner: str | None = None) -> list[dict[str, Any]]:
        """Recently settled asks and who settled them, oldest first.

        Read from HTTP threads while the loop appends, so it copies first: ``tuple()`` of a
        deque runs in C under the GIL, where a Python-level loop over the deque itself could
        see it mutate mid-iteration and raise."""
        return [dict(item) for item in tuple(self._settled)
                if owner is None or item["owner"] == owner]

    def settled_ask(self, ask_id: str) -> dict[str, Any] | None:
        """The record of one settled ask, if it is still remembered."""
        for item in reversed(tuple(self._settled)):
            if item["ask_id"] == ask_id:
                return dict(item)
        return None

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
        self, ask_id: str, approved: bool, *, loop: asyncio.AbstractEventLoop,
        owner: str | None = None, digest: str | None = None,
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
        # Read on the HTTP thread, but owner/action/id never change after parking.
        if owner != ask.owner or digest != ask.digest:
            return "mismatch"

        async def settle() -> bool:
            return self.settle(ask, approved, decided_by=DECIDED_BY_USER)

        try:
            done = asyncio.run_coroutine_threadsafe(settle(), loop).result(timeout=5)
        except RuntimeError:
            # The loop is gone, so whatever it was awaiting is gone with it.
            return "already_resolved"
        return "resolved" if done else "already_resolved"

    def settle(self, ask: PendingAsk, approved: "bool | Unanswered", *,
               decided_by: str = DECIDED_BY_USER) -> bool:
        """Complete one ask. **Must be called on the agent loop** — a future is loop-affine,
        and `set_result` from another thread is undefined behaviour that usually appears to
        work. Returns False if it was already resolved."""
        if ask.future.done():
            return False
        ask.decided_by = decided_by
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
            if self.settle(ask, Unanswered(ApprovalOutcome.DISCONNECTED,
                                           "dashboard stopped polling for this approval"),
                           decided_by=DECIDED_BY_REAPER):
                denied += 1
        return denied

    def forget(self, owner: str) -> int:
        """Deny everything for an owner that is going away. Runs on the agent loop."""
        self._seen.pop(owner, None)
        denied = 0
        for ask in list(self._pending.values()):
            if ask.owner == owner and self.settle(ask, Unanswered(
                    ApprovalOutcome.DISCONNECTED, "dashboard owner went away"),
                    decided_by=DECIDED_BY_CLOSE):
                denied += 1
        return denied

    def __len__(self) -> int:
        return len(self._pending)
