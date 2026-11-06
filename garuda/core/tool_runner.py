"""Tool execution: one call, or a concurrent batch of read-only calls.

Split out of ``core/loop.py``. Everything a tool step needs — environment, tool
map, permissions, hooks, events, the action memo, the side-effect ledger — is held
on the runner instead of threaded through per-call argument lists. The loop asks
for a step and gets a transcript-ready result.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from copy import deepcopy
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
from garuda.tools.protocol import Tool, ToolContext, ToolDispatcher
from garuda.types import Message, Role, ToolCall, ToolResult
from garuda.workspace.health import EnvironmentUnavailableError
from garuda.workspace.protocol import Environment, WorkspaceAccessDenied

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


def _missing_required_arguments(tool, arguments) -> str | None:
    """Feedback for a call missing declared required arguments, else None.

    Reads the tool's own JSON Schema, so it stays correct as tools change and adds
    nothing for a tool that declares no required fields. Returns the message rather
    than raising: a malformed call is the model's to fix on the next turn, not a
    run-ending fault.
    """
    if not isinstance(arguments, dict):
        return (
            f"Tool '{tool.name}' expects a JSON object of arguments, got "
            f"{type(arguments).__name__}."
        )
    schema = getattr(tool, "parameters", None) or {}
    required = schema.get("required") or []
    missing = [name for name in required if name not in arguments]
    if not missing:
        return None
    accepted = list((schema.get("properties") or {}).keys())
    message = (
        f"Tool '{tool.name}' is missing required argument(s): {', '.join(missing)}. "
        f"Accepted arguments: {', '.join(accepted) or 'none'}."
    )
    # Naming what *was* sent is the whole point when the cause is a mistyped key —
    # the model is looking for `pattern` in its own call and sees `pattern:` there.
    if arguments:
        message += f" Received: {', '.join(sorted(map(str, arguments)))}."
    return message


@dataclass
class _ResolvedToolCall(ToolCall):
    tool: Tool


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

    def _error_observation(self, content) -> tuple[str, dict]:
        from garuda.core.artifact_read_model import inline_descriptor, source_capture

        text = content if isinstance(content, str) else str(content)
        shape = self.context.shape_observation_evidence(text, is_error=True)
        session_id = self.ctx.session_id if self.ctx is not None else self.events.session_id
        capture = inline_descriptor(
            shape.content, storage_session_id=session_id, producer="tool-runner",
            source=source_capture(kind="tool_observation", completeness="unknown"),
            omitted_view_bytes=None,
        )
        return shape.content, capture

    def error_message(self, call: ToolCall, content) -> Message:
        """Owned feedback before dispatch, without text-derived buffer authority."""
        text, capture = self._error_observation(content)
        return Message(role=Role.TOOL, content=text, name=call.name, tool_call_id=call.id,
                       metadata={"artifact_capture": capture})

    async def screen(
        self, call: ToolCall, turn: int, *, _dispatching: bool = False, _resolved_tool: Tool | None = None
    ) -> tuple[ToolCall | None, Message | None]:
        """Run permissions and before-hooks for one call.

        Returns ``(call, None)`` when the call may proceed — possibly a call the
        hook rewrote — or ``(None, message)`` with the transcript message to record
        instead. Kept separate from execution so the parallel path can screen
        sequentially (deterministic ordering of denials) and only gather the
        side-effect-free executions.
        """
        denied = await self._authorize(call, turn)
        if denied is not None:
            return None, denied
        # A hook may mutate the call in place or return a new one; keep what was
        # approved so a rewrite can be recognised and decided on its own.
        approved = (call.name, deepcopy(call.arguments))
        hooked_call = await self.hooks.run_before_tool(call, self.hook_context(turn))
        if hooked_call is None:
            return None, self.error_message(call, "Tool call blocked by hook")
        hooked_call.id = call.id
        if (hooked_call.name, hooked_call.arguments) != approved:
            # Approval of the original arguments never covers rewritten ones: the
            # final call needs its own decision, and must name a tool this run has.
            if (hooked_call.name not in self.tool_map
                    and (_resolved_tool is None or hooked_call.name != _resolved_tool.name)):
                return None, self.error_message(
                    call, f"Tool call blocked: a hook rewrote it to unknown tool {hooked_call.name!r}",
                )
            denied = await self._authorize(hooked_call, turn)
            if denied is not None:
                return None, denied
        selected = (_resolved_tool if _resolved_tool is not None and hooked_call.name == _resolved_tool.name
                    else self.tool_map.get(hooked_call.name))
        dispatcher = selected
        if isinstance(dispatcher, ToolDispatcher):
            if _dispatching:
                return None, self.error_message(hooked_call, "Nested use_tool dispatch is not supported.")
            invalid = _missing_required_arguments(dispatcher, hooked_call.arguments)
            if invalid is not None:
                return hooked_call, None  # execute() returns the normal schema error
            resolved = dispatcher.resolve(hooked_call.arguments, self.ctx)
            if isinstance(resolved, ToolResult):
                return None, self.error_message(hooked_call, resolved.content)
            tool, arguments = resolved
            # Preserve the model's call id while giving hooks, events, memo and
            # side-effect accounting the concrete tool name and arguments.
            return await self.screen(ToolCall(id=call.id, name=tool.name, arguments=arguments),
                                     turn, _dispatching=True, _resolved_tool=tool)
        if _resolved_tool is not None and selected is _resolved_tool:
            hooked_call = _ResolvedToolCall(hooked_call.id, hooked_call.name, hooked_call.arguments, selected)
        return hooked_call, None

    async def _authorize(self, call: ToolCall, turn: int) -> Message | None:
        """Permission-check one call; the transcript message when it is refused."""
        allowed, denial_reason = await self.permissions.evaluate_tool_call(
            call.name, call.arguments
        )
        if allowed:
            return None
        # `name`/`id` make a denial attributable. Without them the payload was
        # `{approved, reason}` alone, so a blocked call could not be tied to the
        # call it blocked, and a reader had to collapse four distinct outcomes —
        # denied, hook-blocked, argument-parse failure, never reached — into one
        # "this tool call has no result" state that suggests different fixes.
        self.events.append(
            EventType.PERMISSION_ASK,
            {
                "approved": False,
                "reason": denial_reason,
                "name": call.name,
                "id": call.id,
                "turn": turn,
            },
        )
        return self.error_message(call, denial_reason or "Permission denied")

    def hook_context(self, turn: int) -> dict:
        return {
            "turn": turn,
            "session_id": self.events.session_id,
            "deadline_monotonic": getattr(self.ctx, "deadline_monotonic", None),
        }

    # -- single call ----------------------------------------------------------

    async def run_one(self, call: ToolCall, turn: int) -> ToolResult:
        """Execute one already-screened call, emitting events and appending the result."""
        self.events.append(
            EventType.TOOL_CALL,
            {"id": call.id, "name": call.name, "arguments": call.arguments, "turn": turn},
        )
        tool_result, duration_ms = await self._execute_timed(call)
        # A sequential call's own duration *is* the wall-clock it consumed.
        if self.metrics is not None:
            self.metrics.note_tool_wall(duration_ms)
        capture = self._capture_snapshot(tool_result)
        child = self._child_snapshot(call, tool_result)
        frame = self._frame_snapshot(call, tool_result)
        try:
            tool_result = await self.hooks.run_after_tool(call, tool_result, self.hook_context(turn))
        except asyncio.CancelledError:
            self._record_undelivered(call, capture, duration_ms, turn)
            raise
        self._retain_frame(tool_result, frame)
        self._retain_child_result(call, tool_result, child)
        self._retain_artifact_capture(tool_result, capture)
        if self.ledger is not None:
            self.ledger.observe(call, tool_result)
        self.record(call, tool_result, duration_ms, turn=turn)
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
            if result.is_error and "artifact_capture" not in (result.metadata or {}):
                # Lookup/argument failures and caught exceptions may return before
                # ordinary tool shaping. Their text is observation, never legacy
                # buffer authority. Earlier error shaping may have lost bytes.
                result.content, capture = self._error_observation(result.content)
                result.metadata = dict(result.metadata or {})
                result.metadata["artifact_capture"] = capture
        if self.metrics is not None:
            self.metrics.note_tool(elapsed[0], result.is_error)
        return result, elapsed[0]

    def record(
        self,
        call: ToolCall,
        tool_result: ToolResult,
        duration_ms: float | None = None,
        *,
        turn: int | None = None,
    ) -> None:
        """Emit the result event and append the tool message to the transcript.

        ``turn`` is keyword-only and optional so an out-of-tree caller keeps working;
        both in-tree callers pass it. When it is absent a trajectory reader falls
        back to positional segmentation, so the event is degraded, never wrong.
        """
        message_metadata: dict = {}
        from garuda.core.artifact_read_model import validate_descriptor

        artifact_capture = validate_descriptor(
            (tool_result.metadata or {}).get("artifact_capture")
        )
        if "artifact_capture" in (tool_result.metadata or {}):
            message_metadata["artifact_capture"] = artifact_capture
        if call.name == "invoke_subagent":
            from garuda.core.child_result import (
                canonical_json,
                validate_child_result_envelope,
            )

            child_result = validate_child_result_envelope(
                (tool_result.metadata or {}).get("child_result")
            )
            if (
                child_result is not None
                and tool_result.content.endswith(canonical_json(child_result))
                and len(tool_result.content.encode("utf-8"))
                    <= self.context.output_budget(tool_result.is_error)
            ):
                message_metadata["child_result"] = child_result
            elif child_result is not None or "Validated child result:\n" in tool_result.content:
                tool_result.content = self.context.shape_observation(
                    "Child result delivery unavailable: post-tool processing changed "
                    "the typed handoff or exceeded its output budget.", is_error=True,
                )
                tool_result.is_error = True
                tool_result.metadata.pop("child_result", None)
        if call.name == "buffer_slice":
            frame = (tool_result.metadata or {}).get("buffer_slice_frame")
            receipt_error = (tool_result.metadata or {}).get("buffer_slice_receipt_error")
            receipt = frame if isinstance(frame, dict) else receipt_error
            expected = receipt.get("content_sha256") if isinstance(receipt, dict) else None
            actual = hashlib.sha256(tool_result.content.encode("utf-8")).hexdigest()
            character_mode = "start_char" in (call.arguments or {}) or "end_char" in (
                call.arguments or {}
            )
            receipt_mode = "child_result_reference" in (call.arguments or {})
            missing_contract = (
                character_mode
                and not tool_result.is_error
                and not isinstance(frame, dict)
            ) or (
                receipt_mode
                and tool_result.is_error
                and not isinstance(receipt_error, dict)
            )
            if missing_contract or (expected is not None and expected != actual):
                tool_result.content = self.context.shape_observation(
                    "buffer_slice delivery failed: post-tool processing changed the "
                    "verified character frame.", is_error=True,
                )
                tool_result.is_error = True
                tool_result.metadata.pop("buffer_slice_frame", None)
                tool_result.metadata.pop("buffer_slice_receipt_error", None)
        payload = {
            "tool_call_id": call.id,
            "name": call.name,
            "content": tool_result.content,
            "is_error": tool_result.is_error,
            "turn": turn,
        }
        child_result = message_metadata.get("child_result")
        if child_result is not None:
            # Only validated child metadata is retained alongside the capture
            # contract. Arbitrary tool metadata stays private.
            payload["child_result"] = child_result
        if "artifact_capture" in message_metadata:
            payload["artifact_capture"] = artifact_capture
        if tool_result.is_error and tool_result.metadata.get("permission_denied") is True:
            payload["permission_denied"] = True
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
                metadata=message_metadata,
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
        tool = call.tool if isinstance(call, _ResolvedToolCall) else self.tool_map.get(call.name)
        if tool is None:
            return ToolResult(
                tool_call_id=call.id,
                content=f"Unknown tool: {call.name}",
                is_error=True,
            )
        invalid = _missing_required_arguments(tool, call.arguments)
        if invalid is not None:
            # Checked here rather than left to the tool, because what the tool does
            # with a missing argument is `KeyError: 'pattern'` — which tells the
            # model the name of something it thought it had sent and nothing about
            # what went wrong. Observed live: a model sent `{"pattern:": "*.py"}`,
            # colon inside the key, and burned a turn on an error that never
            # mentioned the key it actually used. Listing the accepted names makes
            # that class of typo self-evident on the first read.
            return ToolResult(tool_call_id=call.id, content=invalid, is_error=True)
        signature = None
        if self.memo is not None:
            signature, _ = self.memo.observe(call)
            cached = self.memo.lookup_result(call, signature)
            if cached is not None:
                self._retain_artifact_capture(
                    cached, cached.metadata.get("artifact_capture"), preserve_view_omissions=True,
                )
                return cached
        try:
            result = await tool.execute(call.arguments, self.env, self.ctx)
            content = result.content if isinstance(result.content, str) else str(result.content)
            result.content = self._shape_or_buffer(content, call, result)
            # Access refusals must be evaluated and recorded on each attempt.
            if (self.memo is not None and signature is not None
                    and result.metadata.get("permission_denied") is not True):
                self.memo.record(
                    call, signature, result.content, result.is_error, result.metadata
                )
        except EnvironmentUnavailableError:
            # Not a tool failure the model can route around — the workspace is
            # gone. Propagate so the run aborts instead of continuing blind.
            raise
        except WorkspaceAccessDenied as exc:
            result = ToolResult(
                tool_call_id=call.id,
                content=self.context.shape_observation(str(exc), is_error=True),
                is_error=True, metadata={"permission_denied": True},
            )
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

    def _shape_or_buffer(self, content, call: ToolCall, result: ToolResult) -> str:
        """Large output → store full body in the buffer + return a stub; else shape inline.

        One budget drives both decisions, and it shrinks as the window fills (see
        ``ContextManager.output_budget``). That ordering is the point: crossing the
        line sends the body to disk with a retrievable stub, which loses nothing,
        while staying under it and then truncating loses the middle for good.

        Never lets a buffer failure crash the turn — falls back to head/tail shaping.
        """
        is_error = result.is_error
        budget = self.context.output_budget(is_error)
        buffer = getattr(self.ctx, "buffer", None)
        from garuda.core.artifact_read_model import (
            failed_descriptor,
            inline_descriptor,
            normalize_source_capture,
        )

        source = normalize_source_capture(
            (result.metadata or {}).get("artifact_source"), default_kind="tool_observation",
            supplied="artifact_source" in (result.metadata or {}),
        )

        def exact_inline() -> str:
            result.metadata["artifact_capture"] = inline_descriptor(
                content, storage_session_id=self.ctx.session_id, producer=call.name,
                source=source, omitted_view_bytes=0,
            )
            return content

        # These two harness-owned contracts have already selected a truthful
        # delivery for this exact live budget. Re-buffering either one would turn
        # a usable reference/frame into another promise to retrieve it.
        if call.name == "invoke_subagent":
            from garuda.core.child_result import (
                canonical_json,
                failed_child_publication_descriptor,
                validate_child_result_envelope,
            )

            envelope = validate_child_result_envelope(
                (result.metadata or {}).get("child_result")
            )
            if envelope is not None:
                if (
                    len(content.encode("utf-8")) <= budget
                    and content.endswith(canonical_json(envelope))
                ):
                    failure = failed_child_publication_descriptor(envelope, self.ctx.session_id)
                    if failure is not None:
                        result.metadata["artifact_capture"] = failure
                    return content
                result.metadata.pop("child_result", None)
                return self.context.shape_observation(
                    "Child result delivery unavailable: the typed handoff did not fit "
                    "the current output budget.",
                    is_error=True,
                )
        if (
            call.name == "buffer_slice"
            and is_error
            and isinstance((result.metadata or {}).get("buffer_slice_receipt_error"), dict)
            and type((result.metadata or {})["buffer_slice_receipt_error"].get("version")) is int
            and (result.metadata or {})["buffer_slice_receipt_error"]["version"] == 1
            and (result.metadata or {})["buffer_slice_receipt_error"].get("content_sha256")
                == hashlib.sha256(content.encode("utf-8")).hexdigest()
            and len(content.encode("utf-8")) <= budget
        ):
            return exact_inline()
        if call.name == "buffer_slice" and not is_error:
            frame = (result.metadata or {}).get("buffer_slice_frame")
            if (
                isinstance(frame, dict)
                and set(frame) == {
                    "version", "start", "end", "total", "complete", "content_sha256"
                }
                and type(frame.get("version")) is int
                and frame["version"] == 1
                and all(type(frame.get(key)) is int for key in ("start", "end", "total"))
                and type(frame.get("complete")) is bool
                and 0 <= frame["start"] <= frame["end"] <= frame["total"]
                and frame["complete"] is (frame["end"] == frame["total"])
                and frame.get("content_sha256")
                    == hashlib.sha256(content.encode("utf-8")).hexdigest()
                and len(content.encode("utf-8")) <= budget
            ):
                return exact_inline()
        if source["completeness"] == "failed":
            # Error text is useful delivery, but it is not the missing source
            # artifact. Retain the failed attempt instead of attesting that text
            # as available log/output bytes, even when ordinary buffering is on.
            capture = failed_descriptor(
                storage_session_id=self.ctx.session_id, producer=call.name,
                source=source, reason=source.get("reason") or "capture_failed",
            )
            if source.get("reason"):
                capture["capture_reason"] = source["reason"]
            result.metadata["artifact_capture"] = capture
            return self.context.shape_observation(content, is_error=is_error)
        # The adaptive budget may only pull the buffering threshold *in*, never push
        # it out: buffer_threshold_bytes is a configured ceiling on what may sit
        # inline, and a run that asked for a small one meant it.
        threshold = min(buffer.threshold_bytes, budget) if buffer is not None else budget
        if buffer is not None and content and buffer.exceeds(content, threshold):
            try:
                from garuda.core.buffer import format_buffer_stub

                # Short, provider-agnostic id (some providers' tool_call ids are ~1KB).
                buffer_id = "buf_" + hashlib.sha1(call.id.encode("utf-8")).hexdigest()[:10]
                ref = buffer.store(
                    buffer_id, content, tool_name=call.name, is_error=is_error,
                    capture=source, producer_session_id=self.ctx.session_id,
                )
                result.metadata["artifact_capture"] = ref.artifact
                return format_buffer_stub(ref)
            except Exception as exc:
                logger.warning(
                    "Buffer store failed for %s: %s; falling back to truncation", call.name, exc
                )
                result.metadata["artifact_capture"] = failed_descriptor(
                    storage_session_id=self.ctx.session_id,
                    producer=call.name,
                    source=source,
                    reason=type(exc).__name__,
                )
                return self.context.shape_observation(content, is_error=is_error)
        shape = self.context.shape_observation_evidence(content, is_error=is_error)
        shaped = shape.content
        omitted_view = shape.omitted_bytes
        result.metadata["artifact_capture"] = inline_descriptor(
            shaped,
            storage_session_id=self.ctx.session_id,
            producer=call.name,
            source=source,
            omitted_view_bytes=omitted_view,
        )
        return shaped

    @staticmethod
    def _frame_snapshot(call: ToolCall, result: ToolResult) -> tuple | None:
        if call.name != "buffer_slice":
            return None
        keys = ("buffer_slice_frame", "buffer_slice_receipt_error")
        metadata = result.metadata or {}
        return (result.content, result.is_error,
                deepcopy({key: metadata[key] for key in keys if key in metadata}))

    def _retain_frame(self, result: ToolResult, original: tuple | None) -> None:
        """Hooks cannot replace the byte selection or receipt selected by the tool."""
        if original is None:
            return
        if not isinstance(result.metadata, dict):
            result.metadata = {}
        keys = ("buffer_slice_frame", "buffer_slice_receipt_error")
        content, is_error, contracts = original
        current = {key: result.metadata[key] for key in keys if key in result.metadata}
        try:
            changed = json.dumps(current, sort_keys=True, allow_nan=False) != json.dumps(
                contracts, sort_keys=True, allow_nan=False,
            )
        except (TypeError, ValueError):
            changed = True
        if changed or (contracts and (
            result.content != content or type(result.is_error) is not type(is_error)
            or result.is_error != is_error
            or len(result.content.encode("utf-8")) > self.context.output_budget(result.is_error)
        )):
            result.content = self.context.shape_observation(
                "buffer_slice delivery failed: post-tool processing changed the verified character frame.",
                is_error=True,
            )
            result.is_error = True
            for key in keys:
                result.metadata.pop(key, None)

    @staticmethod
    def _child_snapshot(call: ToolCall, tool_result: ToolResult) -> dict | None:
        if call.name != "invoke_subagent":
            return None
        from garuda.core.child_result import validate_child_result_envelope

        metadata = tool_result.metadata if isinstance(tool_result.metadata, dict) else {}
        return validate_child_result_envelope(metadata.get("child_result"))

    def _retain_child_result(self, call: ToolCall, tool_result: ToolResult, original: dict | None) -> None:
        """Hooks may render a handoff, never replace its independently validated facts."""
        if call.name != "invoke_subagent":
            return
        if not isinstance(tool_result.metadata, dict):
            tool_result.metadata = {}
        from garuda.core.child_result import canonical_json

        current = self._child_snapshot(call, tool_result)
        changed = (current is None) != (original is None) or (
            current is not None and original is not None
            and canonical_json(current) != canonical_json(original)
        )
        if changed or (original is None and "child_result" in tool_result.metadata):
            tool_result.metadata.pop("child_result", None)
            tool_result.content = self.context.shape_observation(
                "Child result delivery unavailable: post-tool processing changed the validated handoff.",
                is_error=True,
            )
            tool_result.is_error = True
        elif original is not None:
            tool_result.metadata["child_result"] = original

    @staticmethod
    def _capture_snapshot(tool_result: ToolResult) -> dict | None:
        from garuda.core.artifact_read_model import validate_descriptor

        metadata = tool_result.metadata or {}
        if "artifact_capture" not in metadata:
            return None
        return validate_descriptor(metadata["artifact_capture"]) or {}

    def _record_undelivered(
        self, call: ToolCall, capture: object, duration_ms: float | None, turn: int,
    ) -> None:
        """Retain settled owner facts without exposing output awaiting its hooks."""
        from garuda.core.artifact_read_model import validate_descriptor

        retained = validate_descriptor(capture)
        if retained is not None and retained["owner"]["kind"] == "inline-tool-result":
            retained["redaction"]["representation"] = "withheld-tool-result"
            retained["omitted_view_bytes"] = None
            retained["availability"] = {
                "state": "unavailable", "integrity": "unknown", "reason": "delivery_interrupted",
            }
        self.record(call, ToolResult(
            tool_call_id=call.id, content="Tool result delivery interrupted.", is_error=True,
            metadata={"artifact_capture": retained},
        ), duration_ms, turn=turn)

    @staticmethod
    def _retain_artifact_capture(
        tool_result: ToolResult, prior: object, *, preserve_view_omissions: bool = False,
    ) -> None:
        """Post-tool hooks may change rendering, never improve capture evidence."""
        from garuda.core.artifact_read_model import (
            inline_descriptor,
            source_capture,
            validate_descriptor,
        )

        before = validate_descriptor(prior)
        if before is None:
            if prior is not None or "artifact_capture" in tool_result.metadata:
                tool_result.metadata["artifact_capture"] = None
            return
        if before["owner"]["kind"] == "inline-tool-result" and (
            before["digest"].get("sha256")
            != hashlib.sha256(tool_result.content.encode("utf-8")).hexdigest()
        ):
            inherited = source_capture(
                kind=before["source"]["kind"],
                completeness=before["completeness"],
                omitted_source_bytes=before.get("omitted_source_bytes"),
                identity=before["source"].get("identity"),
                media_type=before["media_type"],
                redaction=before["redaction"]["state"],
                reason=before.get("capture_reason") or (None if preserve_view_omissions else "post_tool_processing_changed"),
            )
            tool_result.metadata["artifact_capture"] = inline_descriptor(
                tool_result.content,
                storage_session_id=before["storage_session_id"],
                producer=before.get("producer") or "tool",
                source=inherited,
                omitted_view_bytes=before.get("omitted_view_bytes") if preserve_view_omissions else None,
            )
            return
        # These are the byte owner's facts, independent of a hook's mutable
        # metadata. Hooks may change presentation, never rewrite capture history.
        tool_result.metadata["artifact_capture"] = before

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

        # Hooks (or a rewritten lazy dispatcher) can change a read into a
        # mutation. Reclassify the final calls before dispatching any of them.
        if any(entry[0] == "exec" and entry[1].name not in PARALLEL_SAFE_TOOLS for entry in plan):
            executed = []
            for kind, item in plan:
                if kind == "msg":
                    self.context.append(item)
                else:
                    executed.append((item, await self.run_one(item, turn)))
            return executed

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
                    "turn": turn,
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
        capture_by_index: dict[int, dict | None] = {}
        child_by_index: dict[int, dict | None] = {}
        frame_by_index: dict[int, tuple | None] = {}
        recorded: set[int] = set()

        def retain_undelivered() -> None:
            for index, (kind, item) in enumerate(plan):
                if index in recorded:
                    continue
                if kind == "msg":
                    self.context.append(item)
                else:
                    self._record_undelivered(item, capture_by_index.get(index),
                                           duration_by_index.get(index), turn)
                recorded.add(index)

        async def _bounded(index: int, call: ToolCall) -> ToolResult:
            async with gate:
                result, duration_ms = await self._execute_timed(call)
                duration_by_index[index] = duration_ms
                capture_by_index[index] = self._capture_snapshot(result)
                child_by_index[index] = self._child_snapshot(call, result)
                frame_by_index[index] = self._frame_snapshot(call, result)
                return result

        with stopwatch() as batch_elapsed:
            try:
                results = await asyncio.gather(
                    *(_bounded(i, plan[i][1]) for i in exec_indices),
                    return_exceptions=True,
                )
            except asyncio.CancelledError:
                retain_undelivered()
                raise
        if self.metrics is not None and exec_indices:
            # The segment's wall-clock, against which the summed per-call
            # durations show what overlapping actually recovered.
            self.metrics.note_tool_wall(batch_elapsed[0])
            self.metrics.note_parallel_segment(len(exec_indices))
        # A dead workspace must abort the whole run, not be reported as one
        # read failing; surface it before results are folded into the transcript.
        for outcome in results:
            if isinstance(outcome, EnvironmentUnavailableError):
                retain_undelivered()
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
                recorded.add(i)
                continue
            call = entry[1]
            prior_capture = capture_by_index.get(i)
            try:
                tool_result = await self.hooks.run_after_tool(
                    call, result_by_index[i], self.hook_context(turn)
                )
            except asyncio.CancelledError:
                retain_undelivered()
                raise
            self._retain_frame(tool_result, frame_by_index.get(i))
            self._retain_child_result(call, tool_result, child_by_index.get(i))
            self._retain_artifact_capture(tool_result, prior_capture)
            if self.ledger is not None:
                self.ledger.observe(call, tool_result)
            self.record(call, tool_result, duration_by_index.get(i), turn=turn)
            recorded.add(i)
            executed.append((call, tool_result))
        return executed
