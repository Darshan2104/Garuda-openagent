"""Tool execution: one call, or a concurrent batch of read-only calls.

Split out of ``core/loop.py``. Everything a tool step needs — environment, tool
map, permissions, hooks, events, the action memo, the side-effect ledger — is held
on the runner instead of threaded through per-call argument lists. The loop asks
for a step and gets a transcript-ready result.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from garuda.context.manager import ContextManager
from garuda.context.state_card import WorkingState
from garuda.core.action_memo import ActionMemo
from garuda.core.events import EventStore, EventType
from garuda.core.evidence import is_discriminating
from garuda.core.metrics import RunMetrics, stopwatch
from garuda.core.permissions import PermissionEngine
from garuda.core.side_effects import SideEffectLedger
from garuda.plugins.hooks import HookRegistry
from garuda.tools.protocol import Tool, ToolContext
from garuda.types import Message, Role, ToolCall, ToolResult
from garuda.workspace.health import EnvironmentUnavailableError
from garuda.workspace.protocol import Environment

logger = logging.getLogger(__name__)

# Tools with no side effects, safe to run concurrently within one model response.
# Batching independent reads into one turn is a direct latency/cost win (one model
# round-trip instead of N), so every genuinely read-only tool belongs here.
PARALLEL_SAFE_TOOLS = frozenset(
    {
        "read_file",
        "grep",
        "glob",
        "ls",
        "read_pdf",
        "read_spreadsheet",
        "image_read",
        "web_fetch",
        "web_search",
        "task_output",
        "buffer_grep",
        "buffer_slice",
        "buffer_list",
        "buffer_query",
        "search_tool",
    }
)

# Default ceiling on concurrent reads within one batch, used when no config is
# threaded in (direct ToolRunner construction in tests). Mirrors
# ``AgentConfig.max_parallel_reads``.
DEFAULT_MAX_PARALLEL_READS = 8


@dataclass
class ToolRunner:
    """Executes tool calls against the environment and folds results into the transcript."""

    tool_map: dict[str, Tool]
    env: Environment
    ctx: ToolContext
    context: ContextManager
    permissions: PermissionEngine
    hooks: HookRegistry
    events: EventStore
    memo: ActionMemo | None = None
    ledger: SideEffectLedger | None = None
    metrics: RunMetrics | None = None
    max_parallel_reads: int = DEFAULT_MAX_PARALLEL_READS
    # The run's working state. The runner is the only place that sees every command
    # and every error, so it is where checks and failures get recorded.
    state: WorkingState | None = None

    # -- gating ---------------------------------------------------------------

    async def screen(self, call: ToolCall, turn: int) -> tuple[ToolCall | None, Message | None]:
        """Run permissions and before-hooks for one call.

        Returns ``(call, None)`` when the call may proceed — possibly a call the
        hook rewrote — or ``(None, message)`` with the transcript message to record
        instead. Kept separate from execution so the parallel path can screen
        sequentially (deterministic ordering of denials) and only gather the
        side-effect-free executions.
        """
        allowed, denial_reason = await self.permissions.evaluate_tool_call(
            call.name, call.arguments
        )
        if not allowed:
            self.events.append(
                EventType.PERMISSION_ASK, {"approved": False, "reason": denial_reason}
            )
            return None, Message(
                role=Role.TOOL,
                content=denial_reason or "Permission denied",
                name=call.name,
                tool_call_id=call.id,
            )
        hooked_call = await self.hooks.run_before_tool(call, self.hook_context(turn))
        if hooked_call is None:
            return None, Message(
                role=Role.TOOL,
                content="Tool call blocked by hook",
                name=call.name,
                tool_call_id=call.id,
            )
        hooked_call.id = call.id
        return hooked_call, None

    def hook_context(self, turn: int) -> dict:
        return {"turn": turn, "session_id": self.events.session_id}

    # -- single call ----------------------------------------------------------

    async def run_one(self, call: ToolCall, turn: int) -> ToolResult:
        """Execute one already-screened call, emitting events and appending the result."""
        self.events.append(
            EventType.TOOL_CALL,
            {"id": call.id, "name": call.name, "arguments": call.arguments},
        )
        tool_result, duration_ms = await self._execute_timed(call)
        # A sequential call's own duration *is* the wall-clock it consumed.
        if self.metrics is not None:
            self.metrics.note_tool_wall(duration_ms)
        tool_result = await self.hooks.run_after_tool(call, tool_result, self.hook_context(turn))
        if self.ledger is not None:
            self.ledger.observe(call, tool_result)
        self.record(call, tool_result, duration_ms)
        return tool_result

    async def _execute_timed(self, call: ToolCall) -> tuple[ToolResult, float]:
        """``execute`` plus how long it took, recorded on the run's metrics.

        The single timing point for tool work, so the sequential and concurrent
        paths cannot drift into measuring different things. ``execute`` already
        converts tool exceptions into error results; the only thing that escapes
        it is ``EnvironmentUnavailableError``, which aborts the run, so nothing
        useful is lost by not recording a metric for that case.
        """
        with stopwatch() as elapsed:
            result = await self.execute(call)
        if self.metrics is not None:
            self.metrics.note_tool(elapsed[0], result.is_error)
        return result, elapsed[0]

    def record(self, call: ToolCall, tool_result: ToolResult, duration_ms: float | None = None) -> None:
        """Emit the result event and append the tool message to the transcript."""
        payload = {
            "tool_call_id": call.id,
            "name": call.name,
            "content": tool_result.content,
            "is_error": tool_result.is_error,
        }
        # Consumers previously had to difference the tool_call/tool_result event
        # timestamps to get a duration, which is ~ms-resolution and wrong for a
        # concurrent batch (all its calls are dispatched at the same instant).
        if duration_ms is not None:
            payload["duration_ms"] = duration_ms
        self.events.append(EventType.TOOL_RESULT, payload)
        self._note_state(call, tool_result)
        self.context.append(
            Message(
                role=Role.TOOL,
                content=tool_result.content,
                name=call.name,
                tool_call_id=call.id,
            )
        )

    def _note_state(self, call: ToolCall, tool_result: ToolResult) -> None:
        """Fold this result into the run's working state.

        Only commands whose exit code actually means something are kept as checks —
        ``core/evidence.py`` already knows which those are, and a card listing `ls`
        and `cat` as "checks run" would be worse than one listing none. Bookkeeping,
        so it can never break a turn.
        """
        if self.state is None:
            return
        try:
            if tool_result.is_error:
                self.state.note_failure(call.name, tool_result.content)
            if call.name != "bash":
                return
            command = (call.arguments or {}).get("command")
            exit_code = (tool_result.metadata or {}).get("exit_code")
            if not isinstance(command, str) or not isinstance(exit_code, int):
                return
            if is_discriminating(command):
                record = self.metrics.current if self.metrics is not None else None
                self.state.note_check(command, exit_code, record.turn if record else 0)
        except Exception:
            logger.debug("Working-state bookkeeping failed for %s", call.name, exc_info=True)

    async def execute(self, call: ToolCall) -> ToolResult:
        """Look up and run one tool, shaping or buffering its output."""
        tool = self.tool_map.get(call.name)
        if tool is None:
            return ToolResult(
                tool_call_id=call.id,
                content=f"Unknown tool: {call.name}",
                is_error=True,
            )
        signature = None
        if self.memo is not None:
            signature, _ = self.memo.observe(call)
            cached = self.memo.lookup(call, signature)
            if cached is not None:
                content, is_error = cached
                return ToolResult(tool_call_id=call.id, content=content, is_error=is_error)
        try:
            result = await tool.execute(call.arguments, self.env, self.ctx)
            content = result.content if isinstance(result.content, str) else str(result.content)
            result.content = self._shape_or_buffer(content, call, result.is_error)
            if self.memo is not None and signature is not None:
                self.memo.record(
                    call, signature, result.content, result.is_error, result.metadata
                )
        except EnvironmentUnavailableError:
            # Not a tool failure the model can route around — the workspace is
            # gone. Propagate so the run aborts instead of continuing blind.
            raise
        except Exception as exc:
            logger.warning("Tool %s raised %s: %s", call.name, type(exc).__name__, exc)
            result = ToolResult(
                tool_call_id=call.id,
                content=self.context.shape_observation(
                    f"Tool '{call.name}' failed: {type(exc).__name__}: {exc}", is_error=True
                ),
                is_error=True,
            )
        result.tool_call_id = call.id
        return result

    def _shape_or_buffer(self, content, call: ToolCall, is_error: bool) -> str:
        """Large output → store full body in the buffer + return a stub; else shape inline.

        One budget drives both decisions, and it shrinks as the window fills (see
        ``ContextManager.output_budget``). That ordering is the point: crossing the
        line sends the body to disk with a retrievable stub, which loses nothing,
        while staying under it and then truncating loses the middle for good.

        Never lets a buffer failure crash the turn — falls back to head/tail shaping.
        """
        budget = self.context.output_budget(is_error)
        buffer = getattr(self.ctx, "buffer", None)
        # The adaptive budget may only pull the buffering threshold *in*, never push
        # it out: buffer_threshold_bytes is a configured ceiling on what may sit
        # inline, and a run that asked for a small one meant it.
        threshold = min(buffer.threshold_bytes, budget) if buffer is not None else budget
        if buffer is not None and content and buffer.exceeds(content, threshold):
            try:
                import hashlib

                from garuda.core.buffer import format_buffer_stub

                # Short, provider-agnostic id (some providers' tool_call ids are ~1KB).
                buffer_id = "buf_" + hashlib.sha1(call.id.encode("utf-8")).hexdigest()[:10]
                ref = buffer.store(buffer_id, content, tool_name=call.name, is_error=is_error)
                return format_buffer_stub(ref)
            except Exception as exc:
                logger.warning(
                    "Buffer store failed for %s: %s; falling back to truncation", call.name, exc
                )
        return self.context.shape_observation(content, is_error=is_error)

    # -- parallel batch -------------------------------------------------------

    async def run_parallel_reads(
        self, calls: list[ToolCall], turn: int
    ) -> list[tuple[ToolCall, ToolResult]]:
        """Execute a batch of read-only tool calls concurrently, preserving the
        transcript order of results and each call's tool_call_id pairing.

        Permission checks and before-hooks run sequentially (deterministic
        ordering of denials); only the side-effect-free executions are gathered.

        Returns the executed ``(call, result)`` pairs in the caller's order, so
        the loop can apply the same per-call bookkeeping it applies on the
        sequential path. It used to return bare ``(n_results, n_errors)`` counts,
        which is why a parallel batch silently dropped ``ToolResult.images`` and
        skipped the session-wide repetition steer: neither was reachable from a
        pair of integers. Denied and hook-blocked calls are not in the returned
        list — their transcript message is appended here and there is no result
        to report on.
        """
        plan: list[tuple] = []  # ("msg", Message) | ("exec", call)
        for call in calls:
            screened, message = await self.screen(call, turn)
            if screened is None:
                plan.append(("msg", message))
                continue
            plan.append(("exec", screened))

        exec_indices = [i for i, entry in enumerate(plan) if entry[0] == "exec"]
        # Emit TOOL_CALL at dispatch, not after gather() returns. Trace spans and
        # JSONL consumers derive ordering and timing from these events, so logging
        # the call after its own result made a parallel batch look like it executed
        # before it was requested.
        for index in exec_indices:
            dispatched = plan[index][1]
            self.events.append(
                EventType.TOOL_CALL,
                {
                    "id": dispatched.id,
                    "name": dispatched.name,
                    "arguments": dispatched.arguments,
                },
            )
        # Bound the fan-out. Each read can be a `docker exec` or a subprocess, so
        # a response carrying twenty of them would open twenty at once and spend
        # the saving on self-contention. The gather still covers every call; the
        # semaphore only limits how many are in flight.
        limit = max(1, self.max_parallel_reads)
        gate = asyncio.Semaphore(limit)
        # Durations are collected out-of-band, keyed by plan index: gather() may
        # return an exception in place of a result, so the duration cannot ride on
        # the return value without being lost for exactly the failed calls whose
        # timing is most worth having.
        duration_by_index: dict[int, float] = {}

        async def _bounded(index: int, call: ToolCall) -> ToolResult:
            async with gate:
                result, duration_ms = await self._execute_timed(call)
                duration_by_index[index] = duration_ms
                return result

        with stopwatch() as batch_elapsed:
            results = await asyncio.gather(
                *(_bounded(i, plan[i][1]) for i in exec_indices),
                return_exceptions=True,
            )
        if self.metrics is not None and exec_indices:
            # The segment's wall-clock, against which the summed per-call
            # durations show what overlapping actually recovered.
            self.metrics.note_tool_wall(batch_elapsed[0])
            self.metrics.note_parallel_segment(len(exec_indices))
        # A dead workspace must abort the whole run, not be reported as one
        # read failing; surface it before results are folded into the transcript.
        for outcome in results:
            if isinstance(outcome, EnvironmentUnavailableError):
                raise outcome
        settled: list[ToolResult] = []
        for index, outcome in zip(exec_indices, results, strict=True):
            if isinstance(outcome, BaseException):
                call_id = plan[index][1].id
                logger.warning("Parallel read failed: %s: %s", type(outcome).__name__, outcome)
                outcome = ToolResult(
                    tool_call_id=call_id,
                    content=f"Tool failed: {type(outcome).__name__}: {outcome}",
                    is_error=True,
                )
            settled.append(outcome)
        # strict=True: gather() returns exactly one result per index, and a silent
        # length mismatch would misalign results with their tool_call_ids.
        result_by_index = dict(zip(exec_indices, settled, strict=True))

        executed: list[tuple[ToolCall, ToolResult]] = []
        for i, entry in enumerate(plan):
            if entry[0] == "msg":
                self.context.append(entry[1])
                continue
            call = entry[1]
            tool_result = await self.hooks.run_after_tool(
                call, result_by_index[i], self.hook_context(turn)
            )
            if self.ledger is not None:
                self.ledger.observe(call, tool_result)
            self.record(call, tool_result, duration_by_index.get(i))
            executed.append((call, tool_result))
        return executed
