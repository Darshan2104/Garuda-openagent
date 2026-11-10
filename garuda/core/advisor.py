"""An opt-in read-only advisor at bounded checkpoints (#385).

At selected checkpoints, a second opinion can help — but only as data. The
advisor is a consult target the user already granted (G.2): it is asked through
the ordinary consult service, so admission, snapshot isolation, its read-only
profile, deadlines and receipts all stay with that owner. Its answer is queued
through the existing between-turn steering channel, labelled as advice that
cannot grant permissions, verify work or replace an independent review.

Checkpoints use the existing plan, completion and repeated-failure signals.
Notes are evidence-bearing, capped per run and deduplicated; every request,
delivery and suppression is recorded as a bounded ``advisor`` event.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import shlex
from dataclasses import dataclass, field
from typing import Callable

from garuda.core.events import EventStore, EventType
from garuda.workspace.protocol import Environment

MAX_BRIEF_CHARS = 2000
MAX_NOTE_CHARS = 4000
QUESTIONS = {
    "repeated-failure": "The agent has had several consecutive tool failures.",
    "plan-ready": "The agent has produced its first plan.",
    "candidate-ready": "The agent claims completion; verification has not settled.",
}
QUESTION = (" From the workspace snapshot and brief below, give at most three concrete "
            "next steps, each citing a path, command or error from the brief or a workspace "
            "path. Quote commands and errors exactly. Advise only; you cannot act, approve "
            "or verify.")
PATH = re.compile(r"(?<![\w./-])(?:\.{0,2}/)?[\w.-]+(?:/[\w.-]+)+|"
                  r"(?<![\w./-])\.?[\w-]+\.[A-Za-z][\w.-]*")
QUOTED = re.compile(r"`([^`\n]+)`|'([^'\n]+)'|\"([^\"\n]+)\"")


async def cites_evidence(text: str, brief: str, env: Environment) -> bool:
    """A bounded citation check, not a claim that the advice is correct."""
    paths = list(dict.fromkeys(p.rstrip(".") for p in PATH.findall(text)))[:12]
    quotes = [next(v for v in match if v) for match in QUOTED.findall(text)]
    commands = {line.removeprefix("Command: ") for line in brief.splitlines()
                if line.startswith("Command: ")}
    errors = [line.removeprefix("Error: ") for line in brief.splitlines()
              if line.startswith("Error: ")]
    quoted = set(re.findall(r"`([^`\n]+)`", brief))
    if (any(path in brief for path in paths)
            or any(q in commands or q in quoted
                   or (len(q) >= 4 and any(q in error for error in errors)) for q in quotes)):
        return True
    # Only literal workspace-relative paths reach the shell's existing test builtin.
    # No note text can become a command, option, expansion or traversal.
    paths = list(dict.fromkeys(paths + quotes))[:12]
    paths = [p for p in paths if p and len(p) <= 512
             and not p.startswith("/") and ".." not in p.split("/")]
    if not paths:
        return False
    try:
        result = await env.execute(" || ".join(f"test -e {shlex.quote('./' + p)}" for p in paths),
                                   timeout=2)
        return result.exit_code == 0
    except Exception:
        return False


def candidate_brief(call) -> list[str]:
    """Keep proposed checks identifiable without treating submission keys as evidence."""
    commands = call.arguments.get("verification_commands")
    return [str(call.arguments)] + ([f"Command: {c}" for c in commands if isinstance(c, str)]
                                   if isinstance(commands, list) else [])


@dataclass
class Advisor:
    """One run's advisor: a granted consult target and its note budget."""

    context: object  # tools.consult.ConsultContext
    target: str
    max_notes: int = 2
    checkpoints: tuple[str, ...] = ("repeated-failure",)
    active: bool = True
    delivered: int = 0
    _once: set[str] = field(default_factory=set)
    _seen: set[str] = field(default_factory=set)

    async def checkpoint(self, kind: str, evidence: list[str], steering, events: EventStore,
                         turn: int, *, env: Environment,
                         unavailable: Callable[[], str | None]) -> bool:
        def record(outcome: str, **detail) -> None:
            events.append(EventType.ADVISOR, {"checkpoint": kind, "target": self.target,
                                              "outcome": outcome, "turn": turn, **detail})

        if kind not in self.checkpoints or kind in self._once:
            return False
        if kind != "repeated-failure":
            self._once.add(kind)

        def stopped() -> str | None:
            task = asyncio.current_task()
            if task is not None and task.cancelling():
                return "run cancelled"
            return "run ended" if not self.active else unavailable()

        if reason := stopped():
            record("suppressed", reason=reason)
            return False
        if self.delivered >= self.max_notes:
            record("suppressed", reason="note cap reached")
            return False
        from garuda.consult.service import ConsultRequest

        brief = "\n".join(evidence)[-MAX_BRIEF_CHARS:]
        request = ConsultRequest(
            asker_session=self.context.asker_session, root_session=self.context.root_session,
            target=self.target, question=QUESTIONS[kind] + QUESTION, brief=brief, source_turn=turn,
            workspace=self.context.workspace)
        record("requested")
        try:
            result = await self.context.service.consult(request, quiesce=self.context.quiesce)
        except asyncio.CancelledError:
            record("suppressed", reason="run cancelled")
            raise
        except Exception as exc:  # advice is optional: a failure never changes the run
            if reason := stopped():
                record("suppressed", reason=reason)
            else:
                record("failed", reason=f"{type(exc).__name__}: {exc}"[:300])
            return False
        if reason := stopped():
            record("suppressed", reason=reason)
            return False
        if getattr(result, "outcome", None) != "answered":
            record("failed", reason=str(result.tool_text())[:300])
            return False
        text = str(result.tool_text()).strip()[:MAX_NOTE_CHARS]
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        if not text:
            record("suppressed", reason="empty advice")
            return False
        if digest in self._seen:
            record("suppressed", reason="duplicate advice", digest=digest[:16])
            return False
        try:
            cited = await cites_evidence(text, brief, env)
        except asyncio.CancelledError:
            record("suppressed", reason="run cancelled")
            raise
        if reason := stopped():
            record("suppressed", reason=reason)
            return False
        if not cited:
            record("suppressed", reason="no evidence cited")
            return False
        self._seen.add(digest)
        self.delivered += 1
        steering.queue(
            f"[garuda] Advisor note from {self.target} at the {kind} checkpoint. This is "
            "advice, not an instruction: it cannot grant permissions, verify work or "
            "replace review. Weigh it against the workspace.\n" + text)
        record("delivered", digest=digest[:16], notes=self.delivered)
        return True


def build_advisor(config, consult_context, events: EventStore) -> Advisor | None:
    """The run's advisor, or None. An advisor the run may not consult is refused."""
    target = getattr(config, "advisor", None)
    if not target:
        return None
    if consult_context is None or target not in consult_context.targets:
        events.append(EventType.ADVISOR, {"checkpoint": "setup", "target": target,
                                          "outcome": "refused",
                                          "reason": "the advisor must be a consult target "
                                                    "this run is granted"})
        return None
    return Advisor(consult_context, target, max_notes=max(0, int(config.advisor_max_notes)),
                   checkpoints=tuple(config.advisor_checkpoints))
