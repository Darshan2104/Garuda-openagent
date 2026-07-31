"""Per-turn latency and token metrics for one run.

Existing accounting answers "what did this run cost" (``RunState.usage_totals``,
``eval/costs.py``). Nothing answered "where did the wall-clock go", so every
latency claim about this loop has been computed by hand from outside it — see
``docs/BACKLOG.md`` on the grounding-prompt revision (945s -> 1073s) and the
"wall-clock -16%" figure. Both numbers are real and neither is reproducible from
the harness.

This module makes them reproducible. It records, per turn, how long the model
call took, how long tools took, how much of the tool time was recovered by
running reads concurrently, and how much of the prompt was served from cache.
The rollup lands on ``AgentResult.metadata["metrics"]`` and a ``turn_metrics``
event lands in the event log, so a trajectory carries its own timing.

Deliberately dumb: plain counters, ``time.perf_counter()``, no sampling, no
background thread. Timing instrumentation that can perturb the thing it measures
is worse than none.
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from typing import Iterator


def _ms(seconds: float) -> float:
    """Seconds -> milliseconds, rounded to a useful precision.

    Rounded because these numbers are read by humans and diffed between runs;
    sub-microsecond digits on a network call are noise that makes two equivalent
    runs look different.
    """
    return round(seconds * 1000.0, 3)


@contextmanager
def stopwatch() -> Iterator[list[float]]:
    """Time a block, exposing the elapsed milliseconds as ``holder[0]``.

    A one-element list rather than a returned float because a context manager
    cannot hand back a value that is only known on exit. The elapsed time is
    recorded even when the block raises, so a failed model call still reports how
    long it spent failing — which is exactly the case worth measuring.
    """
    holder = [0.0]
    started = time.perf_counter()
    try:
        yield holder
    finally:
        holder[0] = _ms(time.perf_counter() - started)


@dataclass
class TurnMetrics:
    """One turn's timing and token record."""

    turn: int
    # Model round-trip, measured at the loop's single call site so every Model
    # implementation (litellm, ScriptModel, the eval usage wrapper) is measured
    # the same way. Includes retry/backoff time inside the client, on purpose:
    # that time is wall-clock the run actually spent.
    model_ms: float = 0.0
    # Sum of per-tool execution time. On a concurrent segment this is the sum of
    # each call's own duration, so it exceeds the segment's wall-clock.
    tool_ms_total: float = 0.0
    # Wall-clock actually spent executing tools this turn. Equal to
    # tool_ms_total when everything ran sequentially; less when reads overlapped.
    tool_wall_ms: float = 0.0
    tool_calls: int = 0
    tool_errors: int = 0
    # How the turn's calls were dispatched. parallel_batch_max is the largest
    # concurrent segment; 0 means the turn ran fully sequentially.
    parallel_segments: int = 0
    parallel_batch_max: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    # Prompt tokens served from the provider's cache. Inclusive-of-prompt, matching
    # the convention eval/costs.py already uses (fresh = prompt - cache_read).
    cache_read_tokens: int = 0
    compaction_ms: float = 0.0
    checkpoint_ms: float = 0.0
    # Whether this record's event has been appended yet. A turn that ends the run
    # builds its AgentResult deep inside the tool walk, and that result snapshots the
    # event list — so the event has to be flushed before the snapshot, not after the
    # turn returns, or the last turn's metrics are missing from every consumer that
    # reads metadata["events"] instead of the on-disk log.
    emitted: bool = False

    @property
    def parallel_saved_ms(self) -> float:
        """Tool time recovered by overlapping reads this turn.

        ``tool_ms_total - tool_wall_ms``: the work that happened while other work
        was already in flight. Zero for a sequential turn.
        """
        return max(0.0, round(self.tool_ms_total - self.tool_wall_ms, 3))

    def to_dict(self) -> dict:
        payload = asdict(self)
        # Bookkeeping, not a measurement.
        payload.pop("emitted", None)
        payload["parallel_saved_ms"] = self.parallel_saved_ms
        return payload


def _percentile(values: list[float], fraction: float) -> float:
    """Nearest-rank percentile.

    Not interpolated: with the handful of turns a run produces, interpolation
    invents a number no turn actually took.
    """
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round(fraction * (len(ordered) - 1)))))
    return round(ordered[index], 3)


@dataclass
class RunMetrics:
    """Per-turn records for one run, plus the rollup a caller reads.

    Held on ``RunState`` and shared with ``ToolRunner``: the loop owns turn
    boundaries, the runner owns tool timing, and neither should have to thread
    numbers back through return values to reach the other.
    """

    turns: list[TurnMetrics] = field(default_factory=list)

    def open_turn(self, turn: int) -> TurnMetrics:
        """Start recording a turn and return its (mutable) record."""
        record = TurnMetrics(turn=turn)
        self.turns.append(record)
        return record

    @property
    def current(self) -> TurnMetrics | None:
        """The open turn's record, or None before the first turn.

        ``ToolRunner`` writes tool timing through this rather than being handed a
        record per call: a tool step always belongs to the turn in flight, and
        passing the record down every call path would put a metrics argument on
        four signatures to save one attribute lookup.
        """
        return self.turns[-1] if self.turns else None

    def note_tool(self, duration_ms: float, is_error: bool) -> None:
        record = self.current
        if record is None:
            return
        record.tool_ms_total = round(record.tool_ms_total + duration_ms, 3)
        record.tool_calls += 1
        if is_error:
            record.tool_errors += 1

    def note_tool_wall(self, duration_ms: float) -> None:
        """Add a segment's wall-clock. Called once per dispatched segment."""
        record = self.current
        if record is None:
            return
        record.tool_wall_ms = round(record.tool_wall_ms + duration_ms, 3)

    def note_parallel_segment(self, size: int) -> None:
        record = self.current
        if record is None:
            return
        record.parallel_segments += 1
        record.parallel_batch_max = max(record.parallel_batch_max, size)

    def note_usage(self, usage: dict | None) -> None:
        record = self.current
        if record is None or not usage:
            return
        record.prompt_tokens += int(usage.get("prompt_tokens", 0) or 0)
        record.completion_tokens += int(usage.get("completion_tokens", 0) or 0)
        record.cache_read_tokens += int(usage.get("cache_read_tokens", 0) or 0)

    def summary(self) -> dict:
        """Aggregate rollup for ``AgentResult.metadata["metrics"]``.

        Reports distribution (p50/p95), not just totals: a run whose mean turn is
        fast but whose p95 is 40s has a problem that a mean hides, and that shape
        is the whole reason to measure per turn rather than per run.
        """
        if not self.turns:
            return {"turns": 0}

        model_ms = [t.model_ms for t in self.turns]
        tool_ms = [t.tool_ms_total for t in self.turns]
        prompt_total = sum(t.prompt_tokens for t in self.turns)
        cache_total = sum(t.cache_read_tokens for t in self.turns)
        model_total = round(sum(model_ms), 3)
        tool_wall_total = round(sum(t.tool_wall_ms for t in self.turns), 3)

        return {
            "turns": len(self.turns),
            "model_ms_total": model_total,
            "model_ms_mean": round(model_total / len(self.turns), 3),
            "model_ms_p50": _percentile(model_ms, 0.50),
            "model_ms_p95": _percentile(model_ms, 0.95),
            "tool_ms_total": round(sum(tool_ms), 3),
            "tool_wall_ms_total": tool_wall_total,
            "tool_ms_p50": _percentile(tool_ms, 0.50),
            "tool_ms_p95": _percentile(tool_ms, 0.95),
            "tool_calls": sum(t.tool_calls for t in self.turns),
            "tool_errors": sum(t.tool_errors for t in self.turns),
            "parallel_segments": sum(t.parallel_segments for t in self.turns),
            "parallel_saved_ms": round(sum(t.parallel_saved_ms for t in self.turns), 3),
            "compaction_ms_total": round(sum(t.compaction_ms for t in self.turns), 3),
            "checkpoint_ms_total": round(sum(t.checkpoint_ms for t in self.turns), 3),
            "prompt_tokens": prompt_total,
            "completion_tokens": sum(t.completion_tokens for t in self.turns),
            "cache_read_tokens": cache_total,
            # None rather than 0.0 when no prompt tokens were reported: a provider
            # that omits usage is a different situation from a genuine 0% hit rate,
            # and reporting 0.0 for it would read as a caching regression.
            "cache_hit_rate": (round(cache_total / prompt_total, 4) if prompt_total else None),
        }
