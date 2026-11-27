"""The ``after_turn`` hook of an ACP run: more turns in one run (#819, turn loop).

``run_acp_task`` sends one prompt, then closes the harness, settles the
session's evidence and releases the lease. A caller that passes
``after_turn`` is asked, after each turn, what happens next:

* :meth:`TurnDecision.finish` — settle as today, with this turn's reply as the
  run's reply. After a turn that raised, the error is raised as today, unless
  ``extra["ignore_error"]`` says an earlier turn's result stands (a failed
  repair turn); the session then settles as the earlier turn did.
* :meth:`TurnDecision.next` — send ``prompt`` in the same harness session (it
  must still be idle: a turn that ended ``cancelled`` or raised has closed it).
* :meth:`TurnDecision.rotate` — close this harness session (its child reaped
  and its usage recorded), start a fresh one in the same Garuda session — the
  same lease, evidence baseline and approval broker — re-apply the role's
  options and mode check, and send ``prompt`` with the role's instructions,
  after ``delay_sec``.

The hook sees a :class:`TurnReport` of Garuda's own facts about the turn. It
is called after every turn that returned, whatever its stop reason, and after
a turn that raised an ACP error, except a deadline or silence stop and a usage
limit (``harness.limit_reached``), which always stop as today; a cancellation
(Ctrl-C, SIGTERM) never reaches it. A hook may be sync or async; whatever it
raises fails the run.

Without ``after_turn`` (``garuda run``, the SDK, the ACP server, the runtime
conformance contract) a run sends exactly one prompt, as before.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Union


@dataclass(frozen=True)
class TurnReport:
    #: Prompts sent this run, this one included (1-based).
    turn: int
    #: Prompts sent in the current harness session (1-based).
    session_turn: int
    #: Harness sessions started this run (1 until a rotation).
    harness_sessions: int
    native_session_id: str | None
    #: This turn's reply (the agent's message text, no thoughts).
    reply: str
    #: The harness's raw v1 ``stopReason`` (#824), or None when it sent none.
    stop_reason: str | None
    #: Whether Garuda denied an action during this turn.
    approval_denied: bool
    #: The screened descriptions of the actions Garuda denied this turn (agent text).
    denied: tuple[str, ...] = ()
    #: The harness's last context report this turn, when it sends one.
    context_used: int | None = None
    context_size: int | None = None
    #: The ACP error this turn raised; None for a turn that returned.
    error: BaseException | None = None
    #: Seconds since the run started and since this harness session started.
    elapsed_sec: float = 0.0
    session_elapsed_sec: float = 0.0
    #: Whether the harness session can take another prompt (idle, not closed).
    live: bool = True


@dataclass(frozen=True)
class TurnDecision:
    action: str
    prompt: str = ""
    delay_sec: float = 0.0
    #: Why (recorded by the caller; the mechanism does not read it).
    reason: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def finish(cls, reason: str = "") -> "TurnDecision":
        return cls("finish", reason=reason)

    @classmethod
    def next(cls, prompt: str, reason: str = "") -> "TurnDecision":
        return cls("next", prompt=prompt, reason=reason)

    @classmethod
    def rotate(cls, prompt: str, reason: str = "", delay_sec: float = 0.0) -> "TurnDecision":
        return cls("rotate", prompt=prompt, reason=reason, delay_sec=delay_sec)


AfterTurn = Callable[[TurnReport], Union[TurnDecision, Awaitable[TurnDecision]]]
