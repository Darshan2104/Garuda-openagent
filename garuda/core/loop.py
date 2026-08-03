"""The agent turn loop.

``DefaultAgent.run`` drives turns and nothing else. What it used to also do lives
in four collaborators, assembled by ``prepare_run``:

- ``core/run_state.py`` — setup and the state a run carries (``RunState``)
- ``core/steering.py`` — the messages the harness injects between turns
- ``core/tool_runner.py`` — executing a tool call, or a concurrent read batch
- ``core/completion.py`` — the ``task_complete`` gate

Read this file to learn the control flow; read those to change a behaviour. The
split exists because every fix to any one of those areas used to carry the blast
radius of all four.

Names re-exported here (``CONTINUE_NUDGE``, ``PARALLEL_SAFE_TOOLS``,
``CONTRACT_REJECT_LIMIT``, ``_budget_fraction``) are imported by tests and callers
from this module; they keep working from here.
"""

import logging

from garuda.context.manager import ContextManager
from garuda.core.completion import CONTRACT_REJECT_LIMIT
from garuda.core.events import EventStore, EventType
from garuda.core.metrics import stopwatch
from garuda.core.permissions import PermissionEngine
from garuda.core.run_state import RunState, prepare_run
from garuda.core.steering import (
    CONTEXT_WARNING_FRACTION,
    CONTEXT_WARNING_NUDGE,
    CONTINUE_NUDGE,
    FAILURE_STEER_NUDGE,
    FAILURE_STREAK_THRESHOLD,
    FINAL_SUBMISSION_PROMPT,
    FINAL_TURN_NUDGE,
    REPEAT_NUDGE,
    REPEAT_THRESHOLD,
    TASK_COMPLETE_STUCK_NUDGE,
    TRUNCATION_NOTE,
    batch_signature,
    budget_fraction,
    call_signature,
)
from garuda.core.tool_runner import PARALLEL_SAFE_TOOLS
from garuda.model.litellm_model import TOOL_ARG_PARSE_ERROR_KEY
from garuda.model.protocol import ContextOverflowError, Model
from garuda.plugins.hooks import HookRegistry
from garuda.tools.protocol import Tool
from garuda.types import AgentConfig, AgentResult, Message, Role
from garuda.workspace.health import EnvironmentUnavailableError
from garuda.workspace.protocol import Environment

logger = logging.getLogger(__name__)

# Back-compat aliases: these were module-private helpers in loop.py before the
# split, and are imported by name from tests.
_budget_fraction = budget_fraction
_call_signature = call_signature
_batch_signature = batch_signature

__all__ = [
    "CONTEXT_WARNING_FRACTION",
    "CONTEXT_WARNING_NUDGE",
    "CONTINUE_NUDGE",
    "CONTRACT_REJECT_LIMIT",
    "FAILURE_STEER_NUDGE",
    "FAILURE_STREAK_THRESHOLD",
    "FINAL_SUBMISSION_PROMPT",
    "FINAL_TURN_NUDGE",
    "PARALLEL_SAFE_TOOLS",
    "REPEAT_NUDGE",
    "REPEAT_THRESHOLD",
    "TASK_COMPLETE_STUCK_NUDGE",
    "DefaultAgent",
]


class DefaultAgent:
    def __init__(self, profile_name: str = "build"):
        self._profile_name = profile_name

    @property
    def profile_name(self) -> str:
        return self._profile_name

    async def run(
        self,
        task: str,
        model: Model,
        env: Environment,
        tools: list[Tool],
        config: AgentConfig | None = None,
        events: EventStore | None = None,
        permissions: PermissionEngine | None = None,
        hooks: HookRegistry | None = None,
        subagent_runner=None,
        agents_dir=None,
        context: ContextManager | None = None,
        checkpoint=None,
        buffer=None,
        emit_session_events: bool = True,
        state_checkpoint=None,
    ) -> AgentResult:
        state = await prepare_run(
            task=task,
            profile_name=self._profile_name,
            model=model,
            env=env,
            tools=tools,
            config=config,
            events=events,
            permissions=permissions,
            hooks=hooks,
            subagent_runner=subagent_runner,
            agents_dir=agents_dir,
            context=context,
            checkpoint=checkpoint,
            buffer=buffer,
            emit_session_events=emit_session_events,
            state_checkpoint=state_checkpoint,
        )

        turn = 0
        for turn in range(1, state.config.max_turns + 1):
            # Opened before compaction so this turn's record owns the compaction
            # cost. Compaction happens *because* of the history this turn inherited,
            # and attributing it to the previous turn would make the turn that paid
            # for it look cheap.
            state.metrics.open_turn(turn)

            await state.compact_if_needed(turn)

            state.steering.flush(state.context)
            state.save_checkpoint()

            should_stop, _ = state.steering.open_turn(turn, state.context, state.events)
            if should_stop:
                break

            outcome = await self._run_turn(state, model, turn)
            # Emitted after the turn's work, so a trajectory carries its own timing
            # without a consumer having to difference event timestamps. A turn that
            # ended the run has already flushed its own record (see
            # RunState.flush_turn_metrics), and this is then a no-op.
            state.flush_turn_metrics()
            if outcome is not None:
                return outcome

        return await self._final_submission(state, model, turn)

    async def _final_submission(
        self, state: RunState, model: Model, turn: int
    ) -> AgentResult:
        """Spend one exchange whose only available tool is ``task_complete``.

        The budget is gone and nothing has been accepted. ``FINAL_TURN_NUDGE`` has
        already asked for a commit and been ignored — it is a message, and a message
        cannot make the model stop investigating. Restricting the toolset can: with
        nothing else on offer, "submit or say nothing" is the whole action space.

        What it deliberately does not do is submit *for* the model. The evidence is
        the model's own, run by the ordinary gate against the real workspace, so a
        fabricated command fails here exactly as it would have failed earlier. A
        harness-issued completion would have to invent the evidence it carries, and
        a gate that accepts an empty one is worse than the missing commit.

        Skipped when there is no gate to satisfy (``enable_verifier`` off — those runs
        end on the first tool-free response anyway) or no ``task_complete`` tool. Run
        even past the wall-clock deadline: the deadline margin exists for exactly this
        wind-down, and being inside it is the case this was built for.
        """
        if not (
            state.config.force_final_submission
            and state.config.enable_verifier
            and "task_complete" in state.tool_map
        ):
            return self._exhausted(state, turn)

        turns = min(turn, state.config.max_turns)
        schema = [
            entry
            for entry in state.tools_schema
            if (entry.get("function") or {}).get("name") == "task_complete"
        ]
        if not schema:
            return self._exhausted(state, turn)

        state.events.append(
            EventType.BUDGET,
            {"stage": "final_submission", "turn": turn, "attempted": True},
        )
        # Its own metrics record, labelled. Without one this call's tokens reached
        # `accumulate_usage` while no `turn_metrics` event described them, so anyone
        # reconciling the rollup against total usage was short exactly one call — on
        # precisely the runs that used this mechanism.
        state.metrics.open_turn(turn, label="final_submission")
        state.context.append(Message(role=Role.USER, content=FINAL_SUBMISSION_PROMPT))

        # This is the largest context the run will ever assemble: the last turn's
        # preflight ran before its tool results landed, and the prompt above is
        # appended on top of them. So it is the call most likely to overflow, and
        # going through the ordinary path is what makes the mechanism fire on long
        # runs — which are the runs that exhaust their budget in the first place.
        # Calling `model.complete` raw meant an overflow became a logged warning and
        # the mechanism silently did nothing.
        if state.config.enable_request_preflight:
            await state.compact_if_needed(turn)
        model_ms = [0.0]
        try:
            response = await self._complete_or_shrink(state, model, turn, model_ms, tools=schema)
        except Exception as exc:
            # The run had already failed; a model error here changes nothing about
            # that, so it is logged and the ordinary exhausted result stands.
            logger.warning("Final submission call failed: %s: %s", type(exc).__name__, exc)
            record = state.metrics.current
            if record is not None:
                record.model_ms = model_ms[0]
            state.flush_turn_metrics()
            return self._exhausted(state, turn)

        record = state.metrics.current
        if record is not None:
            record.model_ms = model_ms[0]
        state.accumulate_usage(response.usage)
        state.context.note_usage(response.usage)
        self._record_response(state, response, turn, model_ms[0])

        call = next((c for c in response.tool_calls if c.name == "task_complete"), None)
        if call is None:
            # Nothing submitted. Any calls the response did make (a hallucinated
            # tool name reaches here) are answered first: `_record_response` has
            # already put an assistant message with `tool_calls` in the transcript,
            # and leaving those unanswered is the malformed-transcript bug this
            # change set exists to fix. `_exhausted` then reports the response's
            # content as the run's best answer.
            state.answer_open_calls(
                response.tool_calls, None, reason="Not executed: the run ended here."
            )
            state.flush_turn_metrics()
            return self._exhausted(state, turn)

        try:
            approved, summary = await state.completion.attempt(call)
        except EnvironmentUnavailableError as exc:
            # Nearly moot — the workspace is gone and this transcript will not be
            # replayed — but this is a change set about not leaving open calls, and
            # `AgentResult.messages` is handed out on this path like any other.
            state.answer_open_calls(
                response.tool_calls, None, reason="Not executed: the workspace became unavailable."
            )
            return state.abort_environment_dead(exc, turn)
        if not approved:
            # The gate answered `call` with its rejection feedback; a second
            # task_complete in the same response would still be open.
            state.answer_open_calls(
                response.tool_calls, None, reason="Not executed: the run ended here."
            )
            state.flush_turn_metrics()
            return self._exhausted(state, turn)

        state.answer_open_calls(response.tool_calls, call)
        if state.emit_session_events:
            state.events.append(
                EventType.SESSION_END,
                {"success": True, "turns": turns, "via": "final_submission"},
            )
        return state.result(True, summary, turns)

    async def _timed_complete(
        self, state: RunState, model: Model, model_ms: list[float], tools: list[dict] | None = None
    ):
        """One model call, adding only its own duration to ``model_ms``.

        Accumulated rather than assigned because an overflow turn makes two calls,
        and timed per call rather than around the whole turn so that a compaction
        happening between them is not booked as model latency.

        ``tools`` overrides the run's schema for this call; None means the run's own.
        Used by the final submission turn, whose restricted single-tool schema is the
        whole mechanism — so it cannot simply reuse ``state.tools_schema``.
        """
        elapsed = [0.0]
        try:
            with stopwatch() as holder:
                elapsed = holder
                return await model.complete(
                    state.context.get_messages(),
                    tools=state.tools_schema if tools is None else tools,
                    # Sent, not merely reserved against. run_state.reserved_output_tokens
                    # takes this as the ceiling on the response and shrinks the prompt
                    # budget to match; if the provider were never told, a smaller
                    # max_tokens would shrink the reserve while leaving the response
                    # free to exceed it — making the budget less safe on exactly the
                    # config that reads as tightening it.
                    max_tokens=state.config.max_tokens,
                )
        finally:
            # Runs after the stopwatch's own exit has filled the holder, so this is
            # correct on the raising path too — which is the one that matters here.
            model_ms[0] = round(model_ms[0] + elapsed[0], 3)

    async def _complete_or_shrink(
        self,
        state: RunState,
        model: Model,
        turn: int,
        model_ms: list[float],
        tools: list[dict] | None = None,
    ):
        """The turn's model call, retried once against a forcibly shrunk history.

        The budget is an estimate, and an estimate can be wrong: a tokenizer that
        is not the provider's, an image costed by a flat guess, a resumed session
        whose first prompt was never measured against a provider count. When it is
        wrong in the low direction the provider rejects the request outright.

        Before this, that ended the run — the client correctly does not retry an
        oversized prompt, and the loop had no handler, so a recoverable condition
        was reported as a dead model. Compacting first makes the retry meaningful:
        it is a different, smaller request, not the same one sent twice.
        """
        try:
            return await self._timed_complete(state, model, model_ms, tools=tools)
        except ContextOverflowError:
            before = state.context.budget_snapshot()
            # Timed and booked as compaction, not as model latency. force_compact can
            # make a summarizer call of its own, and an overflow turn is precisely the
            # turn someone later investigates for being slow — charging its compaction
            # to model_ms would send them to the provider for an answer that is here.
            with stopwatch() as compaction_ms:
                shrunk = await state.context.force_compact()
            record = state.metrics.current
            if record is not None:
                record.compaction_ms = round(record.compaction_ms + compaction_ms[0], 3)
            state.events.append(
                EventType.SUMMARIZATION,
                {
                    "turn": turn,
                    "reason": "context_overflow",
                    "recovered": shrunk,
                    "duration_ms": compaction_ms[0],
                    "gauge_said": before["used_tokens"],
                    "capacity": before["capacity_tokens"],
                },
            )
            if not shrunk:
                # Nothing left to drop — an overflow dominated by the system prompt
                # or the tool schemas reaches here. Surfacing the original error is
                # honest: there is no smaller request to make. The attempt is not
                # free, which is why its cost is recorded above rather than hidden.
                raise
            logger.warning(
                "Prompt exceeded the context window (gauge read %s of %s); "
                "force-compacted and retrying once",
                before["used_tokens"],
                before["capacity_tokens"],
            )
            state.reinject_pinned_state()
            return await self._timed_complete(state, model, model_ms, tools=tools)

    async def _run_turn(
        self, state: RunState, model: Model, turn: int
    ) -> AgentResult | None:
        """One model call and its tool steps. Returns a result only if the run ends here."""
        # Preflight. Everything appended since the top-of-turn check — steering
        # nudges, re-pinned state, the previous turn's tool results — is in the
        # prompt now, so this is the first moment the budget can be measured against
        # what is actually about to be sent. Compacting here costs a summary;
        # discovering the overflow in the provider's 400 costs the turn.
        if state.config.enable_request_preflight:
            await state.compact_if_needed(turn)
        state.note_context_budget(turn)

        record = state.metrics.current
        # Timed around each model call rather than inside LitellmModel: one place,
        # and it measures every Model implementation the same way (litellm,
        # ScriptModel, the eval usage-tracking wrapper). Retry/backoff inside the
        # client is included on purpose — that is wall-clock the run actually spent.
        # An overflow turn makes two calls and compacts between them; only the calls
        # land here, so `model_ms` never absorbs compaction time.
        model_ms = [0.0]
        try:
            response = await self._complete_or_shrink(state, model, turn, model_ms)
        except Exception as exc:
            if record is not None:
                record.model_ms = model_ms[0]
            logger.exception("Model call failed after retries")
            if state.emit_session_events:
                state.events.append(
                    EventType.SESSION_END,
                    {
                        "success": False,
                        "reason": "model_error",
                        "error": f"{type(exc).__name__}: {exc}",
                    },
                )
            return state.bare_result(
                False, f"Model call failed: {type(exc).__name__}: {exc}", turn
            )

        if record is not None:
            record.model_ms = model_ms[0]
        state.accumulate_usage(response.usage)
        state.context.note_usage(response.usage)
        self._record_response(state, response, turn, model_ms[0])

        if not response.tool_calls:
            state.final_message = response.content or ""
            if not state.config.enable_verifier:
                if state.emit_session_events:
                    state.events.append(
                        EventType.SESSION_END, {"success": True, "turns": turn}
                    )
                return state.bare_result(True, state.final_message, turn)
            state.context.append(Message(role=Role.USER, content=CONTINUE_NUDGE))
            return None

        try:
            if self._is_parallel_batch(response.tool_calls):
                await self._run_parallel_batch(state, response.tool_calls, turn)
                return None
            return await self._run_calls(state, response.tool_calls, turn)
        except EnvironmentUnavailableError as exc:
            # The more reachable of the two abort paths: any tool in the walk can
            # raise mid-response, so every call after it in that response is left
            # unanswered in the transcript `AgentResult.messages` hands out.
            state.answer_open_calls(
                response.tool_calls, None, reason="Not executed: the workspace became unavailable."
            )
            return state.abort_environment_dead(exc, turn)

    @staticmethod
    def _is_parallel_safe(call) -> bool:
        return (
            call.name in PARALLEL_SAFE_TOOLS
            and TOOL_ARG_PARSE_ERROR_KEY not in call.arguments
        )

    @classmethod
    def _is_parallel_batch(cls, calls) -> bool:
        """True when the whole response is one concurrent read batch.

        Kept as its own fast path even though ``_segment_calls`` would produce the
        same single segment: this case has its own turn-level bookkeeping (one
        failure-streak update, one ``batch_signature`` repetition check) that the
        per-call path does not reproduce.
        """
        return len(calls) > 1 and all(cls._is_parallel_safe(c) for c in calls)

    @classmethod
    def _segment_calls(cls, calls) -> list[tuple[bool, list]]:
        """Split a response's calls into contiguous ``(is_parallel, calls)`` segments.

        Contiguity is what makes this safe. Grouping only *adjacent* read-only calls
        preserves every happens-before relation the model asked for: a read before a
        write still runs before it, a read after it still runs after. The reads that
        end up gathered were already adjacent, and therefore already order-independent
        with respect to each other.

        Before this, one write anywhere in a response forced the whole response
        sequential — including the N independent reads that shared it.

        A lone read is emitted as a sequential segment: a one-item ``gather`` buys
        nothing, and the sequential path is the one that collects images and issues
        the session-wide repetition steer.
        """
        segments: list[tuple[bool, list]] = []
        for call in calls:
            parallel = cls._is_parallel_safe(call)
            if segments and segments[-1][0] == parallel:
                segments[-1][1].append(call)
                continue
            segments.append((parallel, [call]))
        return [
            (parallel and len(group) > 1, group) for parallel, group in segments
        ]

    def _record_response(
        self, state: RunState, response, turn: int, duration_ms: float | None = None
    ) -> None:
        """Log the model response and append it to the transcript."""
        event_payload = {
            "content": response.content,
            "tool_calls": [
                {"id": call.id, "name": call.name, "arguments": call.arguments}
                for call in response.tool_calls
            ],
            "usage": response.usage,
        }
        # Gives observability/tracing.py the extent its `llm` spans lacked: they were
        # emitted with start_time == end_time, so every model call in a trace showed
        # as zero duration.
        if duration_ms is not None:
            event_payload["duration_ms"] = duration_ms
        if response.reasoning_content:
            event_payload["reasoning"] = response.reasoning_content
        state.events.append(EventType.MODEL_RESPONSE, event_payload)

        if response.content and response.content.strip():
            state.last_assistant_text = response.content.strip()
        if response.content or response.tool_calls or response.thinking_blocks:
            assistant_msg = Message(
                role=Role.ASSISTANT,
                content=response.content or "",
                tool_calls=list(response.tool_calls) or None,
            )
            # Retain thinking blocks so they can be echoed back next turn
            # (interleaved thinking must survive across tool-call rounds).
            if response.thinking_blocks:
                assistant_msg.metadata["thinking_blocks"] = response.thinking_blocks
            if response.reasoning_content:
                assistant_msg.metadata["reasoning_content"] = response.reasoning_content
            state.context.append(assistant_msg)

        # Tell the model when its own response was cut off at max_tokens, so it
        # doesn't treat a truncated answer (or truncated tool-call args) as final.
        if response.raw.get("finish_reason") == "length":
            state.events.append(EventType.MODEL_RESPONSE, {"truncated": True, "turn": turn})
            # Queued, not appended: a truncated response often carries a partial
            # tool call, so appending here would split the tool_calls/tool-result
            # pair. Delivered at the top of the next turn instead.
            state.steering.queue(TRUNCATION_NOTE)

    async def _run_parallel_batch(self, state: RunState, calls, turn: int) -> None:
        """Run a whole-response read batch concurrently and update stuck detection."""
        executed = await state.runner.run_parallel_reads(calls, turn)
        # Per-call bookkeeping, exactly as a mixed response gets. Reads used to be
        # graded differently here purely because no write shared the response: one
        # aggregate streak update, and no session-wide memo steer at all — so an
        # agent re-reading the same three files every turn was never told, as long
        # as it read them together.
        for call, tool_result in executed:
            self._note_call_outcome(state, call, tool_result, turn, note_repetition=False)
        # Repetition is the one signal that stays response-level here. Consecutive
        # per-call signatures cannot express "this same batch again": A,B,C,A,B,C
        # never repeats consecutively, so the counter resets every call and the
        # commonest stuck pattern in a read-heavy run goes unnoticed.
        state.steering.note_repetition(batch_signature(calls))
        # image_read is parallel-safe, so a batch can carry images. They used to be
        # dropped here: this path only saw result counts, never the results.
        self._surface_images(state, [img for _, result in executed for img in result.images])

    @staticmethod
    def _surface_images(state: RunState, images: list[str]) -> None:
        """Show tool-returned images to the model as one (portable) user message.

        Appended after every tool result so the assistant's ``tool_calls`` block and
        its results stay contiguous. Dropped by the model layer for non-vision models.
        """
        if not images:
            return
        state.context.append(
            Message(
                role=Role.USER,
                content=f"[{len(images)} image(s) returned by tools]",
                images=images,
            )
        )

    async def _run_calls(self, state: RunState, calls, turn: int) -> AgentResult | None:
        """Run this turn's calls in order, gathering adjacent read-only runs.

        Returns a result only if the run ends here. Ordering across segments is the
        order the model asked for; see ``_segment_calls`` for why that is what makes
        concurrency safe here.
        """
        turn_images: list[str] = []
        for parallel, group in self._segment_calls(calls):
            if parallel:
                for call, tool_result in await state.runner.run_parallel_reads(group, turn):
                    turn_images.extend(tool_result.images)
                    self._note_call_outcome(state, call, tool_result, turn, note_repetition=False)
                continue
            # The whole response's calls travel with the segment: an accepted
            # completion has to answer every call in the assistant message it came
            # from, including those in segments this run will now never reach.
            outcome = await self._run_group_sequentially(
                state, group, turn, turn_images, response_calls=calls
            )
            if outcome is not None:
                return outcome

        # Repetition is reported once for the whole response, whatever shape it had.
        # Per-call signatures cannot express "this same response again": a repeated
        # [read_a, read_b, write] yields a, b, w, a, b, w and never repeats
        # consecutively, so the commonest mixed stuck pattern went unseen. For a
        # single-call response batch_signature is exactly call_signature, so nothing
        # changes there.
        state.steering.note_repetition(batch_signature(calls))
        self._surface_images(state, turn_images)
        return None

    def _note_call_outcome(
        self, state: RunState, call, tool_result, turn: int, note_repetition: bool = True
    ) -> None:
        """Per-call bookkeeping, shared by every path a tool result can arrive on.

        Shared deliberately: the concurrent path used to skip all of this, so a
        gathered read never contributed to the failure streak and never triggered the
        session-wide repetition steer.

        Repetition is the one signal not reported here in practice: both callers pass
        ``note_repetition=False`` and report once per *response* instead, because a
        repeated multi-call response never repeats consecutively call-by-call. The
        parameter stays for a caller with a single call and no response context.
        """
        state.steering.record_failure_streak(
            had_error=tool_result.is_error,
            had_success=not tool_result.is_error,
            events=state.events,
            turn=turn,
        )
        state.steering.note_progress()  # progress made; reset completion-retry counter

        signature = call_signature(call)
        if note_repetition:
            state.steering.note_repetition(signature)
        # Session-wide (not just consecutive) repetition: the same call
        # coming back many turns later is the more common waste pattern.
        steer = state.memo.steer_note(call, signature, state.memo.count_for(signature))
        if steer:
            state.steering.queue(steer)

    async def _run_group_sequentially(
        self, state: RunState, calls, turn: int, turn_images: list[str], response_calls=None
    ) -> AgentResult | None:
        """Run one segment's calls one at a time. Returns a result only if the run ends.

        ``response_calls`` is the full set of calls from the assistant message this
        segment belongs to, used only to close the transcript when a completion is
        accepted. Defaults to this segment's calls for callers with no wider context.
        """
        for call in calls:
            if call.name == "task_complete":
                approved, summary = await state.completion.attempt(call)
                if approved:
                    # Close the response's tool_calls block before returning: the
                    # accepted call and any siblings this run will now never reach
                    # would otherwise sit unanswered in the transcript this result
                    # carries. See RunState.answer_open_calls.
                    state.answer_open_calls(response_calls or calls, call)
                    if state.emit_session_events:
                        state.events.append(
                            EventType.SESSION_END, {"success": True, "turns": turn}
                        )
                    return state.result(True, summary, turn)
                state.steering.note_completion_rejection()
                continue

            if TOOL_ARG_PARSE_ERROR_KEY in call.arguments:
                state.context.append(
                    Message(
                        role=Role.TOOL,
                        content=call.arguments[TOOL_ARG_PARSE_ERROR_KEY],
                        name=call.name,
                        tool_call_id=call.id,
                    )
                )
                continue

            screened, message = await state.runner.screen(call, turn)
            if screened is None:
                state.context.append(message)
                continue
            call = screened

            tool_result = await state.runner.run_one(call, turn)
            turn_images.extend(tool_result.images)
            self._note_call_outcome(state, call, tool_result, turn, note_repetition=False)
        return None

    def _exhausted(self, state: RunState, turn: int) -> AgentResult:
        """Budget exhausted without an accepted completion.

        Reports the agent's last substantive output rather than a bare "max turns
        exceeded" — after the forced final turn it usually holds the best answer the
        run produced, and discarding it loses work that was actually done.
        """
        reason = "deadline" if state.steering.deadline_exceeded() else "max_turns"
        turns = min(turn, state.config.max_turns)
        if state.emit_session_events:
            state.events.append(
                EventType.SESSION_END,
                {
                    "success": False,
                    "reason": reason,
                    "turns": turns,
                    "final_answer_offered": bool(state.last_assistant_text),
                },
            )
        return state.result(
            False,
            state.final_message
            or state.last_assistant_text
            or "Budget exhausted before the task was verified.",
            turns,
        )
