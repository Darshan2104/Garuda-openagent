"""Own terminal approval presentation and one stdin reader per event loop.

Cancellation cannot stop input() in a thread. On a POSIX terminal the reader
thread therefore waits in select() until a whole line is ready and only then
calls input(), so a withdrawn prompt (answered from another terminal or the
dashboard) stops its reader without consuming input or keeping the process
alive. Elsewhere (pipes, Windows) a withdrawn read cannot be stopped: retain its
ownership so the next approval discards that read, even if the line arrived
between prompts. Previews resume when a cancelled presentation exits, while that
orphan input() thread may still own stdin until the next presentation drains it.

Single-loop only: all approval users of one stdin must share the same event
loop, as the CLI does. Concurrent loops in different threads are not serialized.
Do not replace the loop while a withdrawn input reader is outstanding: closing
its loop cannot stop its thread, which can consume input intended for a later loop.
"""

from __future__ import annotations

import asyncio
import os
import select
import sys
import threading
import uuid
import weakref
from dataclasses import dataclass, field

from garuda.core.approval_outcome import ApprovalOutcome, Unanswered
from garuda.interfaces.tui import suspend_previews


@dataclass
class _TerminalState:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    read: asyncio.Future | None = None
    owner: str | None = None
    reader: _LineReader | None = None


def _stoppable_stdin() -> int | None:
    """Return stdin's fd when a pending read of it can be stopped (a POSIX tty)."""
    if os.name == "nt" or sys.stdin is None:
        return None
    try:
        return sys.stdin.fileno() if sys.stdin.isatty() else None
    except (AttributeError, OSError, ValueError):
        return None


class _LineReader:
    """One input() call off the loop; stop() ends it if no line was taken yet.

    With a stoppable fd the thread waits for a line (canonical tty mode reports
    readable only for a complete line) or a wake-up byte before calling input(),
    so stop() never leaves a thread blocked in input() that would keep the
    process from exiting. Without one it is the plain executor read.
    """

    def __init__(self, loop: asyncio.AbstractEventLoop):
        self._fd = _stoppable_stdin()
        if self._fd is None:
            self.future = asyncio.ensure_future(asyncio.to_thread(input))
            return
        self._loop = loop
        self.future = loop.create_future()
        self._lock = threading.Lock()
        self._done = False
        self._wake_r, self._wake_w = os.pipe()
        threading.Thread(target=self._run, name="garuda-approval-stdin").start()

    def _run(self) -> None:
        try:
            try:
                select.select([self._fd, self._wake_r], [], [])
            finally:
                with self._lock:
                    stopped, self._done = self._done, True
                    os.close(self._wake_r)
                    os.close(self._wake_w)
            if stopped:
                return
            result, error = input(), None
        except BaseException as exc:  # delivered to the awaiting presenter
            result, error = None, exc
        try:
            self._loop.call_soon_threadsafe(self._settle, result, error)
        except RuntimeError:  # loop already closed; nobody awaits this line
            pass

    def _settle(self, result, error) -> None:
        if self.future.done():
            return
        if error is not None:
            self.future.set_exception(error)
        else:
            self.future.set_result(result)

    def stop(self) -> bool:
        """End the read before it takes a line; False if input() is or was called."""
        if self._fd is None:
            return False
        with self._lock:
            if self._done:
                return False
            self._done = True
            os.write(self._wake_w, b"x")
        self.future.cancel()
        return True


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
            with suspend_previews():
                self._print(text)
                try:
                    if state.read is not None and state.owner != key:
                        # A completed read also belongs to the withdrawn prompt.
                        await self._read_line(state)
                        self._print("\n[garuda] discarded input for a withdrawn approval; answer again:\n")
                        self._print(text)
                    state.reader = _LineReader(loop)
                    state.read, state.owner = state.reader.future, key
                    line = await self._read_line(state)
                except asyncio.CancelledError:
                    if state.owner == key and state.reader.stop():
                        state.read, state.owner, state.reader = None, None, None
                        self._print("\n[garuda] approval withdrawn\n")
                    else:
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
            state.read, state.owner, state.reader = None, None, None
            raise
        state.read, state.owner, state.reader = None, None, None
        return line
