"""Run-scoped terminal-tool strategies for the native agent loop.

The loop owns *when* a terminal call can end a run.  A strategy owns *which*
tool is terminal and whether that call is accepted.  Keeping that boundary
small lets specialized child runs use a structured submission tool without
copying the native loop or weakening normal ``task_complete`` verification.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from garuda.core.completion import CompletionGate
from garuda.types import ToolCall


@dataclass(frozen=True)
class TerminalDecision:
    """The outcome of evaluating one terminal-tool call."""

    accepted: bool
    summary: str = ""


class TerminalStrategy(Protocol):
    """The per-run policy used to recognize and evaluate terminal calls.

    A rejected strategy must append the terminal call's tool result before
    returning, just as :class:`CompletionGate` does.  On acceptance the loop
    closes the accepted call and any unexecuted siblings before it asks the
    strategy to flush deferred USER-role notes.
    """

    tool_name: str
    supports_forced_submission: bool

    async def attempt(
        self, call: ToolCall, *, turn: int | None = None
    ) -> TerminalDecision: ...

    def flush_notes(self) -> None: ...


@dataclass
class TaskCompletionStrategy:
    """Adapt the existing verified ``task_complete`` gate to the strategy seam."""

    completion_gate: CompletionGate
    tool_name: str = "task_complete"
    supports_forced_submission: bool = True

    async def attempt(
        self, call: ToolCall, *, turn: int | None = None
    ) -> TerminalDecision:
        accepted, summary = await self.completion_gate.attempt(call, turn=turn)
        return TerminalDecision(accepted=accepted, summary=summary)

    def flush_notes(self) -> None:
        self.completion_gate.flush_notes()
