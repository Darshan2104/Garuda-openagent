"""Session-wide memory of tool calls already made.

The loop's existing repeat detector compares each call against the *immediately
preceding* one, so it only catches back-to-back repetition. Most wasted work is
not back-to-back: an agent re-reads the same file, or re-runs the same expensive
command, many turns apart, having forgotten it already has the answer.

Two mechanisms, deliberately separated by safety:

* **Memo** — for tools that cannot change the workspace, return the earlier
  observation instead of executing again. Any mutating call invalidates the
  whole memo, so a cached read can never outlive the state it described.
* **Repetition counter** — for *every* tool, count how often each exact
  signature has been issued across the session and steer the model once a call
  keeps coming back. Mutating calls are never served from cache; re-running them
  may be legitimate, so the intervention is advice, not substitution.
"""

import json
import logging
from dataclasses import dataclass, field

from garuda.types import ToolCall

logger = logging.getLogger(__name__)

# Tools with no workspace side effects, safe to serve from cache. A superset of
# the loop's PARALLEL_SAFE_TOOLS minus anything whose value is time-dependent.
CACHEABLE_TOOLS = frozenset(
    {
        "read_file",
        "grep",
        "glob",
        "ls",
        "read_pdf",
        "read_spreadsheet",
        "image_read",
        "buffer_grep",
        "buffer_slice",
        "buffer_list",
        "buffer_query",
    }
)

# Tools that never touch the workspace and therefore must not invalidate the
# memo when they run (bookkeeping, planning, completion signalling).
NON_MUTATING_TOOLS = CACHEABLE_TOOLS | frozenset(
    {
        "todo",
        "update_goal",
        "task_complete",
        "contract",
        "think",
        "web_search",
        "web_fetch",
        "search_tool",
        "task_output",
    }
)

# How many times an identical signature may appear before the model is steered.
REPEAT_STEER_THRESHOLD = 3

REPEAT_STEER_NOTE = (
    "[repetition] You have now issued this exact call {count} times in this session "
    "(first at step {first}): {label}\n"
    "You already have its result — re-running it will not produce new information. "
    "Use what you learned the first time, or change the call so it answers a question "
    "you have not yet answered."
)

CACHE_HIT_SUFFIX = (
    "\n\n[cached] Identical call already made at step {first}; the workspace has not been "
    "modified since, so this is the same result. Avoid re-reading unchanged state."
)


def call_signature(call: ToolCall) -> str:
    try:
        args = json.dumps(call.arguments, sort_keys=True, default=str)
    except (TypeError, ValueError):
        args = str(call.arguments)
    return f"{call.name}:{args}"


def call_label(call: ToolCall, limit: int = 160) -> str:
    """Short human-readable rendering of a call, for steering messages."""
    args = call.arguments or {}
    for key in ("command", "path", "pattern", "file_path"):
        if isinstance(args.get(key), str):
            text = args[key].strip().replace("\n", " ")
            return f"{call.name}({text[:limit]})"
    try:
        blob = json.dumps(args, sort_keys=True, default=str)
    except (TypeError, ValueError):
        blob = str(args)
    return f"{call.name}({blob[:limit]})"


@dataclass
class _Entry:
    content: str
    is_error: bool
    first_step: int


@dataclass
class ActionMemo:
    """Per-session record of what has already been asked and answered."""

    _entries: dict[str, _Entry] = field(default_factory=dict)
    _counts: dict[str, int] = field(default_factory=dict)
    _first_seen: dict[str, int] = field(default_factory=dict)
    _steered: set[str] = field(default_factory=set)
    step: int = 0
    hits: int = 0
    invalidations: int = 0

    def observe(self, call: ToolCall) -> tuple[str, int]:
        """Record that ``call`` is about to run. Returns (signature, occurrence)."""
        self.step += 1
        signature = call_signature(call)
        count = self._counts.get(signature, 0) + 1
        self._counts[signature] = count
        self._first_seen.setdefault(signature, self.step)
        return signature, count

    def lookup(self, call: ToolCall, signature: str) -> tuple[str, bool] | None:
        """Cached (content, is_error) for a repeated read-only call, else None."""
        if call.name not in CACHEABLE_TOOLS:
            return None
        entry = self._entries.get(signature)
        if entry is None:
            return None
        self.hits += 1
        return entry.content + CACHE_HIT_SUFFIX.format(first=entry.first_step), entry.is_error

    def record(self, call: ToolCall, signature: str, content: str, is_error: bool) -> None:
        """Store a result and, if the call mutated anything, drop cached reads."""
        if call.name in CACHEABLE_TOOLS:
            self._entries.setdefault(
                signature, _Entry(content=content, is_error=is_error, first_step=self.step)
            )
            return
        if call.name not in NON_MUTATING_TOOLS:
            self.invalidate()

    def count_for(self, signature: str) -> int:
        """How many times this exact signature has been issued this session."""
        return self._counts.get(signature, 0)

    def invalidate(self) -> None:
        """Forget every cached read (the workspace may have changed)."""
        if self._entries:
            self.invalidations += 1
            self._entries.clear()

    def steer_note(self, call: ToolCall, signature: str, count: int) -> str | None:
        """A nudge when a signature keeps recurring — emitted once per signature."""
        if count < REPEAT_STEER_THRESHOLD or signature in self._steered:
            return None
        self._steered.add(signature)
        return REPEAT_STEER_NOTE.format(
            count=count, first=self._first_seen.get(signature, 1), label=call_label(call)
        )

    def stats(self) -> dict[str, int]:
        repeats = sum(count - 1 for count in self._counts.values() if count > 1)
        total = sum(self._counts.values())
        return {
            "distinct_calls": len(self._counts),
            "total_calls": total,
            "repeat_calls": repeats,
            "cache_hits": self.hits,
            "invalidations": self.invalidations,
        }
