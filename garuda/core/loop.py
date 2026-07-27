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
from garuda.core.permissions import PermissionEngine
from garuda.core.run_state import RunState, prepare_run
from garuda.core.steering import (
    CONTEXT_WARNING_FRACTION,
    CONTEXT_WARNING_NUDGE,
    CONTINUE_NUDGE,
    FAILURE_STEER_NUDGE,
    FAILURE_STREAK_THRESHOLD,
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
from garuda.model.protocol import Model
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
    "FINAL_TURN_NUDGE",
    "PARALLEL_SAFE_TOOLS",
    "REPEAT_NUDGE",
    "REPEAT_THRESHOLD",
    "TASK_COMPLETE_STUCK_NUDGE",
    "DefaultAgent",
]


def _tools_schema(tools: list[Tool]) -> list[dict]:
    return [
        {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.parameters,
            },
        }
        for tool in tools
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
        )

        turn = 0
        for turn in range(1, state.config.max_turns + 1):
            if await state.context.maybe_summarize():
                state.events.append(EventType.SUMMARIZATION, {"turn": turn})
                # Compaction can summarize away the goal and todo list; re-pin them
                # so long tasks keep their north star and don't re-derive their plan
                # (which would waste turns and tokens).
                state.reinject_pinned_state()

            state.steering.flush(state.context)
            state.save_checkpoint()

            should_stop, _ = state.steering.open_turn(turn, state.context, state.events)
            if should_stop:
                break

            outcome = await self._run_turn(state, model, turn)
            if outcome is not None:
                return outcome

        return self._exhausted(state, turn)

    async def _run_turn(
        self, state: RunState, model: Model, turn: int
    ) -> AgentResult | None:
        """One model call and its tool steps. Returns a result only if the run ends here."""
        try:
            response = await model.complete(
                state.context.get_messages(),
                tools=_tools_schema(state.tools),
            )
        except Exception as exc:
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

        state.accumulate_usage(response.usage)
        state.context.note_usage(response.usage)
        self._record_response(state, response, turn)

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
            return await self._run_sequential_calls(state, response.tool_calls, turn)
        except EnvironmentUnavailableError as exc:
            return state.abort_environment_dead(exc, turn)

    @staticmethod
    def _is_parallel_batch(calls) -> bool:
        return len(calls) > 1 and all(
            c.name in PARALLEL_SAFE_TOOLS and TOOL_ARG_PARSE_ERROR_KEY not in c.arguments
            for c in calls
        )

    def _record_response(self, state: RunState, response, turn: int) -> None:
        """Log the model response and append it to the transcript."""
        event_payload = {
            "content": response.content,
            "tool_calls": [
                {"id": call.id, "name": call.name, "arguments": call.arguments}
                for call in response.tool_calls
            ],
            "usage": response.usage,
        }
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
        """Run a batch of read-only calls concurrently and update stuck detection."""
        n_results, n_errors = await state.runner.run_parallel_reads(calls, turn)
        state.steering.record_failure_streak(
            had_error=n_errors > 0,
            had_success=(n_results - n_errors) > 0,
            events=state.events,
            turn=turn,
        )
        state.steering.note_progress()
        # Detect an identical parallel batch repeated turn after turn (a common
        # stuck pattern) the same way single-call repetition is caught.
        state.steering.note_repetition(batch_signature(calls))

    async def _run_sequential_calls(
        self, state: RunState, calls, turn: int
    ) -> AgentResult | None:
        """Run this turn's calls in order. Returns a result only if the run ends here."""
        turn_images: list[str] = []
        for call in calls:
            if call.name == "task_complete":
                approved, summary = await state.completion.attempt(call)
                if approved:
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
            if tool_result.images:
                turn_images.extend(tool_result.images)

            state.steering.record_failure_streak(
                had_error=tool_result.is_error,
                had_success=not tool_result.is_error,
                events=state.events,
                turn=turn,
            )
            state.steering.note_progress()  # progress made; reset completion-retry counter

            signature = call_signature(call)
            state.steering.note_repetition(signature)
            # Session-wide (not just consecutive) repetition: the same call
            # coming back many turns later is the more common waste pattern.
            steer = state.memo.steer_note(call, signature, state.memo.count_for(signature))
            if steer:
                state.steering.queue(steer)

        # Surface any images returned by this turn's tools to the model as a
        # (portable) user image message, appended after all tool results so the
        # tool_calls/result block stays contiguous. Dropped by the model layer
        # for non-vision models.
        if turn_images:
            state.context.append(
                Message(
                    role=Role.USER,
                    content=f"[{len(turn_images)} image(s) returned by tools]",
                    images=turn_images,
                )
            )
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
