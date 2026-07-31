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
from garuda.core.action_memo import ActionMemo
from garuda.core.events import EventStore, EventType
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
        tool_result = await self.execute(call)
        tool_result = await self.hooks.run_after_tool(call, tool_result, self.hook_context(turn))
        if self.ledger is not None:
            self.ledger.observe(call, tool_result)
        self.record(call, tool_result)
        return tool_result

    def record(self, call: ToolCall, tool_result: ToolResult) -> None:
        """Emit the result event and append the tool message to the transcript."""
        self.events.append(
            EventType.TOOL_RESULT,
            {
                "tool_call_id": call.id,
                "name": call.name,
                "content": tool_result.content,
                "is_error": tool_result.is_error,
            },
        )
        self.context.append(
            Message(
                role=Role.TOOL,
                content=tool_result.content,
                name=call.name,
                tool_call_id=call.id,
            )
        )

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

        Never lets a buffer failure crash the turn — falls back to head/tail shaping.
        """
        buffer = getattr(self.ctx, "buffer", None)
        if buffer is not None and content and buffer.exceeds(content):
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

    async def run_parallel_reads(self, calls: list[ToolCall], turn: int) -> tuple[int, int]:
        """Execute a batch of read-only tool calls concurrently, preserving the
        transcript order of results and each call's tool_call_id pairing.

        Permission checks and before-hooks run sequentially (deterministic
        ordering of denials); only the side-effect-free executions are gathered.
        Returns ``(n_results, n_errors)`` for failure-streak tracking.
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
        results = await asyncio.gather(
            *(self.execute(plan[i][1]) for i in exec_indices),
            return_exceptions=True,
        )
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

        n_results = 0
        n_errors = 0
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
            n_results += 1
            if tool_result.is_error:
                n_errors += 1
            self.record(call, tool_result)
        return n_results, n_errors
