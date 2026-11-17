"""Own terminal approval presentation and one stdin reader per event loop.

Cancellation cannot stop input() in a thread. Retain its ownership so the next
approval discards that read, even if the line arrived between prompts.

Single-loop only: all approval users of one stdin must share the same event
loop, as the CLI does. Concurrent loops in different threads are not serialized.
Do not replace the loop while a withdrawn input reader is outstanding: closing
its loop cannot stop its thread, which can consume input intended for a later loop.
"""

from __future__ import annotations

import asyncio
import uuid
import weakref
from dataclasses import dataclass, field

from garuda.core.approval_outcome import ApprovalOutcome, Unanswered


@dataclass
class _TerminalState:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    read: asyncio.Task | None = None
    owner: str | None = None


_states: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


class TerminalApprover:
    """Single-loop stdin callback with optional request-aware worker labels."""

    def __init__(self, stream):
        self.stream = stream

    async def __call__(self, action: str) -> bool | Unanswered:
        return await self._present(uuid.uuid4().hex, self._root_prompt(action))

    @staticmethod
    def _root_prompt(action: str) -> str:
        return f"\n[garuda] Approve {action}? [y/N]: "

    async def ask_request(self, request) -> bool | Unanswered:
        requester = request.requester
        if requester is None:
            text = self._root_prompt(request.action)
        else:
            kind = "worker" if requester["kind"] == "editing_worker" else "subagent"
            queue = f" ({request.queue_length} waiting)" if request.queue_length is not None else ""
            text = (f"\n[garuda] approval{queue}: "
                    f"{kind} {requester['session_id'][:12]} · agent {requester['agent']} "
                    f"· depth {requester['depth']}\n  {request.action}\nApprove? [y/N]: ")
        # Unique per presentation, including across brokers with caller-supplied ids.
        return await self._present(uuid.uuid4().hex, text)

    def _print(self, text: str) -> None:
        print(text, end="", flush=True, file=self.stream)

    async def _present(self, key: str, text: str) -> bool | Unanswered:
        loop = asyncio.get_running_loop()
        state = _states.setdefault(loop, _TerminalState())
        async with state.lock:
            self._print(text)
            try:
                if state.read is not None and state.owner != key:
                    # A completed read also belongs to the withdrawn prompt.
                    await self._read_line(state)
                    self._print("\n[garuda] discarded input for a withdrawn approval; answer again:\n")
                    self._print(text)
                state.read = asyncio.create_task(asyncio.to_thread(input))
                state.owner = key
                line = await self._read_line(state)
            except asyncio.CancelledError:
                self._print("\n[garuda] approval withdrawn; the next line typed is discarded\n")
                raise
            except EOFError:
                return Unanswered(ApprovalOutcome.DISCONNECTED, "terminal input closed")
            return line.strip().lower() in ("y", "yes")

    @staticmethod
    async def _read_line(state: _TerminalState) -> str:
        try:
            line = await asyncio.shield(state.read)
        except asyncio.CancelledError:
            # Keep the input task and owner for the next presenter to drain.
            raise
        except BaseException:
            state.read, state.owner = None, None
            raise
        state.read, state.owner = None, None
        return line
