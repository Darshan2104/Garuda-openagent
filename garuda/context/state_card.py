"""What the run knows about itself, as structured state rather than prose.

Before this, the only thing that survived a compaction was a markdown blob the
model re-derived on every summarize: it could forget a file it had edited, invent
a test result, or quietly drop a criterion. That is a strange thing to pay a model
call for, because the harness already holds most of those facts exactly —
``SideEffectLedger`` knows which files were written, ``UpdateGoalTool`` and
``TodoTool`` hold the plan, ``AcceptanceContract`` holds the criteria, and the
tool runner sees every command and its exit code.

So the card is assembled from those sources and rendered deterministically. The
model's summary keeps only the part no ledger can produce — findings, dead ends,
and why — which is both a better use of the call and a much smaller prompt.

Every list is hard-bounded. A state card that grows with the run would reintroduce
the problem it exists to solve.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Bounds. Chosen so a fully-loaded card lands in the low hundreds of tokens: the
# card is re-pinned after every compaction and handed to every brief subagent, so
# its size is paid many times over a run.
MAX_FILES = 40
MAX_CHECKS = 20
MAX_FAILURES = 5
MAX_NARRATIVE_CHARS = 4_000
MAX_TODOS = 30
# The task is clipped like everything else. It is not lost by doing so: the full
# statement is the first user message, which every condenser preserves verbatim.
# What the card carries is an identifying reminder, and an unbounded one would make
# the card grow with the task — the exact failure it exists to prevent.
MAX_TASK_CHARS = 1_500
MAX_FAILURE_CHARS = 300
MAX_COMMAND_CHARS = 200

STATE_CARD_HEADER = "[working state — maintained by the harness, survives compaction]"


@dataclass
class CheckRecord:
    """One verification command the run actually executed, and how it exited."""

    command: str
    exit_code: int
    turn: int = 0

    @property
    def passed(self) -> bool:
        return self.exit_code == 0

    def render(self) -> str:
        mark = "✓" if self.passed else "✗"
        return f"  {mark} `{self.command}` → exit {self.exit_code} (turn {self.turn})"

    def to_dict(self) -> dict:
        return {"command": self.command, "exit_code": self.exit_code, "turn": self.turn}


@dataclass
class WorkingState:
    """The run's deterministic state, plus one slot the model maintains.

    ``narrative`` is the only field a model writes. Everything above it is observed,
    which is what makes the card trustworthy enough to re-pin without re-checking.
    """

    task: str = ""
    goal: str = ""
    todos: list[dict] = field(default_factory=list)
    files_modified: list[str] = field(default_factory=list)
    checks: list[CheckRecord] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    acceptance: str = ""
    narrative: str = ""

    # -- recording ------------------------------------------------------------

    def note_check(self, command: str, exit_code: int, turn: int = 0) -> None:
        """Record a verification command's outcome, newest wins.

        Deduplicated by command text: a test suite run eight times should occupy one
        line reporting where it stands now, not eight lines of history. The *latest*
        exit code is the true one — an earlier failure that has since been fixed is
        exactly the stale fact a card must not carry.
        """
        command = _clip(command.strip(), MAX_COMMAND_CHARS)
        if not command:
            return
        self.checks = [c for c in self.checks if c.command != command]
        self.checks.append(CheckRecord(command=command, exit_code=exit_code, turn=turn))
        del self.checks[:-MAX_CHECKS]

    def note_failure(self, tool: str, message: str) -> None:
        """Record a tool error. Deduplicated, most recent last."""
        entry = f"{tool}: {_clip(' '.join((message or '').split()), MAX_FAILURE_CHARS)}"
        if entry in self.failures:
            self.failures.remove(entry)
        self.failures.append(entry)
        del self.failures[:-MAX_FAILURES]

    # -- rendering ------------------------------------------------------------

    def render(self) -> str:
        """The card as it appears in context. Empty sections are omitted, not blank —
        a heading with nothing under it costs tokens and says nothing."""
        parts: list[str] = [STATE_CARD_HEADER]
        _section(parts, "Task", _clip(self.task, MAX_TASK_CHARS))
        _section(parts, "Goal", self.goal)
        if self.todos:
            _section(parts, "Todos", _render_todos(self.todos))
        if self.files_modified:
            shown = self.files_modified[:MAX_FILES]
            more = len(self.files_modified) - len(shown)
            body = "\n".join(f"  - {path}" for path in shown)
            if more > 0:
                body += f"\n  … and {more} more"
            _section(parts, f"Files modified ({len(self.files_modified)})", body)
        if self.checks:
            _section(
                parts,
                "Checks run",
                "\n".join(check.render() for check in self.checks[-MAX_CHECKS:]),
            )
        if self.failures:
            _section(
                parts,
                "Recent failures",
                "\n".join(f"  - {failure}" for failure in self.failures),
            )
        _section(parts, "Acceptance criteria", self.acceptance)
        _section(parts, "Notes", _clip(self.narrative, MAX_NARRATIVE_CHARS))
        return "\n".join(parts)

    def is_empty(self) -> bool:
        """True when the card would say nothing beyond the task it started with."""
        return not any(
            (self.goal, self.todos, self.files_modified, self.checks, self.failures,
             self.acceptance, self.narrative)
        )

    # -- persistence ----------------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "task": self.task,
            "goal": self.goal,
            "todos": list(self.todos),
            "files_modified": list(self.files_modified),
            "checks": [check.to_dict() for check in self.checks],
            "failures": list(self.failures),
            "acceptance": self.acceptance,
            "narrative": self.narrative,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "WorkingState":
        """Rebuild from a checkpoint. Tolerant of a partial or older document —
        a resume must not fail because one field was added since it was written."""
        data = data or {}
        checks = []
        for raw in data.get("checks") or []:
            try:
                checks.append(
                    CheckRecord(
                        command=str(raw.get("command", "")),
                        exit_code=int(raw.get("exit_code", 0)),
                        turn=int(raw.get("turn", 0)),
                    )
                )
            except (TypeError, ValueError, AttributeError):
                continue
        return cls(
            task=str(data.get("task") or ""),
            goal=str(data.get("goal") or ""),
            todos=list(data.get("todos") or []),
            files_modified=list(data.get("files_modified") or []),
            checks=checks,
            failures=[str(f) for f in (data.get("failures") or [])],
            acceptance=str(data.get("acceptance") or ""),
            narrative=str(data.get("narrative") or ""),
        )


def _clip(text: str, limit: int) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    return text[:limit] + f"… [+{len(text) - limit} chars]"


def _section(parts: list[str], title: str, body: str) -> None:
    if body and body.strip():
        parts.append(f"## {title}\n{body.strip()}")


def _render_todos(todos: list[dict]) -> str:
    from garuda.tools.todo import render_todos

    rendered = render_todos(todos[:MAX_TODOS])
    if len(todos) > MAX_TODOS:
        rendered += f"\n… and {len(todos) - MAX_TODOS} more"
    return rendered
