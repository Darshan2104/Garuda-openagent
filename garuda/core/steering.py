"""Steering: what the harness tells the model between turns.

Split out of ``core/loop.py``. This module owns every message the harness injects
that is *not* a tool result — budget notices, stuck-pattern nudges, the forced
final turn — plus the counters that decide when to inject them.

The one invariant that governs the whole file: a steering note is **queued**, not
appended. Appending mid-turn would insert a user message between an assistant
``tool_calls`` message and its tool results, which providers reject with a 400.
Notes are therefore flushed at the *top* of the next turn, when the previous
turn's tool-result block is complete and contiguous. Anything added here must
respect that — call ``queue()``, never ``context.append()``.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field

from garuda.context.manager import ContextManager
from garuda.core.events import EventStore, EventType
from garuda.types import Message, Role, ToolCall

logger = logging.getLogger(__name__)

CONTINUE_NUDGE = (
    "You responded without calling a tool. If the task is finished, call task_complete "
    "with a summary; otherwise continue working using the available tools."
)

REPEAT_NUDGE = (
    "You have now executed the exact same tool call {count} times in a row with identical "
    "arguments. Repeating it again is unlikely to change the outcome. Step back, reconsider "
    "your approach, and try something different (different arguments, a different tool, or "
    "inspect the environment to understand why it is not working)."
)

REPEAT_THRESHOLD = 3

# After this many consecutive failing tool steps (all errors, any arguments),
# steer the model toward a different approach.
FAILURE_STREAK_THRESHOLD = 3

FAILURE_STEER_NUDGE = (
    "The last {count} tool calls all failed. Stop repeating the same approach: re-check your "
    "assumptions, try a different tool or path (for example fall back to `bash` with an explicit "
    "path if a structured tool keeps failing), or inspect the environment to find out why."
)

TASK_COMPLETE_STUCK_NUDGE = (
    "task_complete has now been rejected {count} times in a row. Do not just resubmit — "
    "address the specific verifier feedback above (run the missing checks, fix the gap, or "
    "disambiguate the answer) before calling task_complete again."
)

CONTEXT_WARNING_FRACTION = 0.8

CONTEXT_WARNING_NUDGE = (
    "[budget] The context window is over {percent}% full. Be economical: avoid re-reading "
    "large files, prefer targeted grep/read with offsets, and summarize instead of dumping "
    "output. Older messages will be compacted soon — before that happens, write durable "
    "notes (current plan, key findings, decisions, remaining steps, important paths) to a "
    "scratch file in the workspace (e.g. `.agent_notes.md`) so you can re-read them after "
    "compaction."
)

# Fractions of the run's budget at which the agent is made to re-examine its
# approach. A long run that is going nowhere rarely notices on its own: each
# individual turn looks locally reasonable, so the prompt to step back has to
# come from outside the reasoning that produced them.
BUDGET_REVIEW_STAGES = (0.5, 0.8)

BUDGET_REVIEW_NUDGE = (
    "[budget] {percent}% of this run's budget is spent ({turns_used}/{max_turns} turns"
    "{clock}). Before your next action, state plainly:\n"
    "  1. What have you established as fact, with the evidence for it?\n"
    "  2. What have you ruled out, and how?\n"
    "  3. Is your current approach still the best use of the remaining budget, or has it "
    "stopped producing new information?\n"
    "  4. If it has stopped: what is the different approach, or the best answer you can "
    "commit to with what you already know?\n"
    "Answer these before continuing. Repeating the last {turns_used} turns' pattern for the "
    "remaining budget is the most likely way to finish with nothing."
)

FINAL_TURN_NUDGE = (
    "[budget] This is your FINAL turn — the run ends after it, and an unfinished run scores "
    "nothing regardless of how much was learned.\n"
    "Commit the best answer you have now: write any required output files with your best "
    "current values, then call task_complete. State honestly in the summary what is verified "
    "and what is a best guess. A committed imperfect answer beats no answer."
)

TRUNCATION_NOTE = (
    "[note] Your previous response was truncated at the output-token limit. "
    "Continue where you left off, or make the remaining work more concise."
)


def turn_budget_notice(turn: int, max_turns: int) -> str | None:
    remaining = max_turns - turn
    if remaining == max(1, max_turns // 4):
        return (
            f"[budget] {remaining} of {max_turns} turns remain. Prioritize finishing the core "
            "task; avoid exploratory detours and call task_complete once the work is verified."
        )
    if remaining == 5:
        return (
            "[budget] Only 5 turns remain. Wrap up now: make the smallest change that "
            "completes the task and call task_complete with a summary."
        )
    return None


def budget_fraction(
    turn: int, max_turns: int, started_at: float, deadline_at: float | None
) -> float:
    """Share of the run's budget consumed, whichever of turns/clock is further along."""
    by_turns = turn / max(1, max_turns)
    if deadline_at is None:
        return by_turns
    total = deadline_at - started_at
    if total <= 0:
        return 1.0
    return max(by_turns, (time.monotonic() - started_at) / total)


def call_signature(call: ToolCall) -> str:
    try:
        args = json.dumps(call.arguments, sort_keys=True, default=str)
    except (TypeError, ValueError):
        args = str(call.arguments)
    return f"{call.name}:{args}"


def batch_signature(calls: list[ToolCall]) -> str:
    """Order-independent signature for a parallel tool batch, so an identical
    repeated batch is caught by the same repetition detector as a single call."""
    return "||".join(sorted(call_signature(c) for c in calls))


@dataclass
class Steering:
    """Per-run steering state and the decisions that read it.

    Holds the counters that used to be loose locals in ``DefaultAgent.run``:
    repetition, failure streak, completion-rejection streak, budget review stage,
    and the queued-note list.
    """

    max_turns: int
    started_at: float
    deadline_at: float | None

    pending_notes: list[str] = field(default_factory=list)
    last_signature: str | None = None
    repeat_count: int = 0
    failure_streak: int = 0
    completion_rejections: int = 0
    context_warned: bool = False
    budget_stage: int = 0
    final_turn_forced: bool = False
    final_turn_at: int = 0

    # -- notes ----------------------------------------------------------------

    def queue(self, note: str) -> None:
        """Queue a note for delivery at the top of the next turn. See module docstring."""
        self.pending_notes.append(note)

    def flush(self, context: ContextManager) -> None:
        """Deliver queued notes, now that the previous turn's tool-result block is closed."""
        for note in self.pending_notes:
            context.append(Message(role=Role.USER, content=note))
        self.pending_notes.clear()

    # -- budget ---------------------------------------------------------------

    def spent_fraction(self, turn: int) -> float:
        return budget_fraction(turn, self.max_turns, self.started_at, self.deadline_at)

    def open_turn(
        self, turn: int, context: ContextManager, events: EventStore
    ) -> tuple[bool, float]:
        """Run the start-of-turn budget and context checks.

        Returns ``(should_stop, spent_fraction)``. ``should_stop`` is True once the
        reserved final turn has been used — the run ends there.

        These messages append directly rather than queueing, which is safe: this
        runs at the top of a turn, before any tool call of this turn exists.
        """
        notice = turn_budget_notice(turn, self.max_turns)
        if notice:
            context.append(Message(role=Role.USER, content=notice))

        spent = self.spent_fraction(turn)
        while (
            self.budget_stage < len(BUDGET_REVIEW_STAGES)
            and spent >= BUDGET_REVIEW_STAGES[self.budget_stage]
        ):
            threshold = BUDGET_REVIEW_STAGES[self.budget_stage]
            self.budget_stage += 1
            elapsed = time.monotonic() - self.started_at
            clock = f", {elapsed:.0f}s elapsed" if self.deadline_at else ""
            events.append(
                EventType.BUDGET,
                {"stage": threshold, "turn": turn, "elapsed_sec": round(elapsed, 1)},
            )
            context.append(
                Message(
                    role=Role.USER,
                    content=BUDGET_REVIEW_NUDGE.format(
                        percent=int(threshold * 100),
                        turns_used=turn,
                        max_turns=self.max_turns,
                        clock=clock,
                    ),
                )
            )

        # Reserve the last turn for committing an answer. Without this the run
        # can end mid-investigation having produced nothing, which is strictly
        # worse than an honest partial result.
        if self.final_turn_forced and turn > self.final_turn_at:
            return True, spent
        if not self.final_turn_forced and (turn == self.max_turns or spent >= 1.0):
            self.final_turn_forced = True
            self.final_turn_at = turn
            events.append(EventType.BUDGET, {"stage": "final_turn", "turn": turn})
            context.append(Message(role=Role.USER, content=FINAL_TURN_NUDGE))

        if not self.context_warned and context.usage_fraction() >= CONTEXT_WARNING_FRACTION:
            self.context_warned = True
            context.append(
                Message(
                    role=Role.USER,
                    content=CONTEXT_WARNING_NUDGE.format(
                        percent=int(CONTEXT_WARNING_FRACTION * 100)
                    ),
                )
            )
        return False, spent

    def deadline_exceeded(self) -> bool:
        return bool(self.deadline_at and time.monotonic() >= self.deadline_at)

    # -- stuck-pattern detection ---------------------------------------------

    def note_repetition(self, signature: str) -> None:
        """Track consecutive identical steps and queue a nudge at the threshold."""
        if signature == self.last_signature:
            self.repeat_count += 1
        else:
            self.last_signature = signature
            self.repeat_count = 1
        if self.repeat_count >= REPEAT_THRESHOLD:
            self.queue(REPEAT_NUDGE.format(count=self.repeat_count))
            self.repeat_count = 0
            self.last_signature = None

    def record_failure_streak(
        self, had_error: bool, had_success: bool, events: EventStore, turn: int
    ) -> None:
        """Update the consecutive-failure streak for a tool step and steer if stuck.

        A step with any success resets the streak; an all-error step increments it.
        At the threshold a steering nudge is queued (delivered at the next turn's top,
        so it never splits a tool_calls/tool-result pair) and the streak resets.
        """
        if had_success:
            self.failure_streak = 0
            return
        if had_error:
            self.failure_streak += 1
        if self.failure_streak >= FAILURE_STREAK_THRESHOLD:
            self.queue(FAILURE_STEER_NUDGE.format(count=self.failure_streak))
            events.append(
                EventType.TOOL_RESULT,
                {"failure_streak": self.failure_streak, "steered": True, "turn": turn},
            )
            self.failure_streak = 0

    def note_progress(self) -> None:
        """A tool step did something; the completion-retry counter no longer applies."""
        self.completion_rejections = 0

    def note_completion_rejection(self) -> None:
        """A rejected task_complete. Steer if the model keeps resubmitting instead of
        acting on the verifier feedback — task_complete is otherwise exempt from the
        repeat/failure detectors."""
        self.completion_rejections += 1
        if self.completion_rejections >= REPEAT_THRESHOLD:
            self.queue(TASK_COMPLETE_STUCK_NUDGE.format(count=self.completion_rejections))
            self.completion_rejections = 0
