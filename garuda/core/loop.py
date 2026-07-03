import asyncio
import json
import logging

from garuda.context.manager import ContextManager
from garuda.core.events import EventStore, EventType
from garuda.core.permissions import PermissionEngine
from garuda.core.verifier import CompletionVerifier
from garuda.model.litellm_model import TOOL_ARG_PARSE_ERROR_KEY
from garuda.model.protocol import Model
from garuda.plugins.hooks import HookRegistry
from garuda.tools.protocol import Tool, ToolContext
from garuda.types import (
    DEFAULT_SYSTEM_PROMPT,
    AgentConfig,
    AgentResult,
    Message,
    Role,
    ToolCall,
    ToolResult,
)
from garuda.workspace.protocol import Environment

logger = logging.getLogger(__name__)

CONTINUE_NUDGE = (
    "You responded without calling a tool. If the task is finished, call task_complete "
    "with a summary; otherwise continue working using the available tools."
)

REPEAT_NUDGE = (
    "You have now executed the exact same tool call {count} times in a row with identical "
    "arguments. Repeating it again is unlikely to change the outcome. Step back, reconsider "
    "your approach, and try something different (different arguments, a different tool, or "
    "inspect the environment to understand why it is not working)."
)

REPEAT_THRESHOLD = 3

# After this many consecutive failing tool steps (all errors, any arguments),
# steer the model toward a different approach.
FAILURE_STREAK_THRESHOLD = 3

FAILURE_STEER_NUDGE = (
    "The last {count} tool calls all failed. Stop repeating the same approach: re-check your "
    "assumptions, try a different tool or path (for example fall back to `bash` with an explicit "
    "path if a structured tool keeps failing), or inspect the environment to find out why."
)

CONTEXT_WARNING_FRACTION = 0.8

# Tools with no side effects, safe to run concurrently within one model response.
PARALLEL_SAFE_TOOLS = frozenset(
    {
        "read_file",
        "grep",
        "glob",
        "ls",
        "read_pdf",
        "read_spreadsheet",
        "web_fetch",
        "web_search",
        "task_output",
    }
)


def _turn_budget_notice(turn: int, max_turns: int) -> str | None:
    remaining = max_turns - turn
    if remaining == max(1, max_turns // 4):
        return (
            f"[budget] {remaining} of {max_turns} turns remain. Prioritize finishing the core "
            "task; avoid exploratory detours and call task_complete once the work is verified."
        )
    if remaining == 5:
        return (
            "[budget] Only 5 turns remain. Wrap up now: make the smallest change that "
            "completes the task and call task_complete with a summary."
        )
    return None


def _call_signature(call: ToolCall) -> str:
    try:
        args = json.dumps(call.arguments, sort_keys=True, default=str)
    except (TypeError, ValueError):
        args = str(call.arguments)
    return f"{call.name}:{args}"


def _accumulate_usage(totals: dict[str, int], usage: dict[str, int]) -> None:
    for key, value in (usage or {}).items():
        totals[key] = totals.get(key, 0) + int(value)


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
        self._verifier = CompletionVerifier()

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
    ) -> AgentResult:
        config = config or AgentConfig()
        events = events or EventStore()
        permissions = permissions or PermissionEngine(mode=config.permission_mode)
        hooks = hooks or HookRegistry()
        events.append(EventType.SESSION_START, {"task": task, "model": model.model_name})

        tool_map = {tool.name: tool for tool in tools}
        if config.allowed_tools:
            allowed = set(config.allowed_tools)
            for tool in tools:
                if tool.name.startswith("mcp__"):
                    allowed.add(tool.name)
            tool_map = {name: tool_map[name] for name in allowed if name in tool_map}
            tools = list(tool_map.values())

        if context is None:
            system_prompt = config.system_prompt or DEFAULT_SYSTEM_PROMPT
            context = ContextManager(
                model=model,
                max_output_bytes=config.max_output_bytes,
                proactive_threshold=config.proactive_summarize_threshold,
                max_context_tokens=config.max_context_tokens,
                enable_three_step_summary=config.enable_three_step_summary,
                task=task,
                condenser=config.condenser,
            )
            context.seed(
                [
                    Message(role=Role.SYSTEM, content=system_prompt),
                    Message(role=Role.USER, content=task),
                ]
            )
        events.append(EventType.USER_MESSAGE, {"content": task})

        if subagent_runner is None and "invoke_subagent" in tool_map:
            from garuda.core.subagent import SubagentRunner

            subagent_runner = SubagentRunner(
                model=model,
                env=env,
                events=events,
                agents_dir=agents_dir,
                skills_dirs=config.skills_dirs,
                workspace_root=getattr(env, "workspace_root", None),
                parent_context=context,
                approval_handler=permissions.approval_handler,
                hooks=hooks,
            )

        buffer = None
        if config.buffer_tool_output:
            from garuda.core.buffer import ToolOutputBuffer

            buffer = ToolOutputBuffer(
                session_id=events.session_id,
                threshold_bytes=config.buffer_threshold_bytes,
            )

        ctx = ToolContext(
            session_id=events.session_id,
            agent_profile=self._profile_name,
            model=model,
            subagent_runner=subagent_runner,
            buffer=buffer,
        )
        final_message = ""
        usage_totals: dict[str, int] = {}
        last_signature: str | None = None
        repeat_count = 0
        failure_streak = 0
        context_warned = False

        for turn in range(1, config.max_turns + 1):
            if await context.maybe_summarize():
                events.append(EventType.SUMMARIZATION, {"turn": turn})

            budget_notice = _turn_budget_notice(turn, config.max_turns)
            if budget_notice:
                context.append(Message(role=Role.USER, content=budget_notice))
            if not context_warned and context.usage_fraction() >= CONTEXT_WARNING_FRACTION:
                context_warned = True
                context.append(
                    Message(
                        role=Role.USER,
                        content=(
                            "[budget] The context window is over "
                            f"{int(CONTEXT_WARNING_FRACTION * 100)}% full. Be economical: avoid "
                            "re-reading large files, prefer targeted grep/read with offsets, and "
                            "summarize instead of dumping output."
                        ),
                    )
                )

            try:
                response = await model.complete(
                    context.get_messages(),
                    tools=_tools_schema(tools),
                )
            except Exception as exc:
                logger.exception("Model call failed after retries")
                events.append(
                    EventType.SESSION_END,
                    {"success": False, "reason": "model_error", "error": f"{type(exc).__name__}: {exc}"},
                )
                return self._result(
                    False,
                    f"Model call failed: {type(exc).__name__}: {exc}",
                    context,
                    turn,
                    events,
                    usage_totals,
                )

            _accumulate_usage(usage_totals, response.usage)
            context.note_usage(response.usage)
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
            events.append(EventType.MODEL_RESPONSE, event_payload)

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
                context.append(assistant_msg)

            # Tell the model when its own response was cut off at max_tokens, so it
            # doesn't treat a truncated answer (or truncated tool-call args) as final.
            if response.raw.get("finish_reason") == "length":
                events.append(EventType.MODEL_RESPONSE, {"truncated": True, "turn": turn})
                context.append(
                    Message(
                        role=Role.USER,
                        content=(
                            "[note] Your previous response was truncated at the output-token limit. "
                            "Continue where you left off, or make the remaining work more concise."
                        ),
                    )
                )

            if not response.tool_calls:
                final_message = response.content or ""
                if not config.enable_verifier:
                    events.append(EventType.SESSION_END, {"success": True, "turns": turn})
                    return self._result(True, final_message, context, turn, events, usage_totals)
                context.append(Message(role=Role.USER, content=CONTINUE_NUDGE))
                continue

            if len(response.tool_calls) > 1 and all(
                c.name in PARALLEL_SAFE_TOOLS and TOOL_ARG_PARSE_ERROR_KEY not in c.arguments
                for c in response.tool_calls
            ):
                n_results, n_errors = await self._run_parallel_reads(
                    response.tool_calls, tool_map, env, ctx, context, permissions, hooks, events, turn
                )
                failure_streak = self._record_failure_streak(
                    failure_streak,
                    had_error=n_errors > 0,
                    had_success=(n_results - n_errors) > 0,
                    context=context,
                    events=events,
                    turn=turn,
                )
                last_signature = None
                repeat_count = 0
                continue

            for call in response.tool_calls:
                if call.name == "task_complete":
                    completed = await self._handle_task_complete(
                        call, task, context, env, config, events, turn, usage_totals,
                        permissions, model,
                    )
                    if completed is not None:
                        return completed
                    continue

                if TOOL_ARG_PARSE_ERROR_KEY in call.arguments:
                    context.append(
                        Message(
                            role=Role.TOOL,
                            content=call.arguments[TOOL_ARG_PARSE_ERROR_KEY],
                            name=call.name,
                            tool_call_id=call.id,
                        )
                    )
                    continue

                allowed, denial_reason = await permissions.evaluate_tool_call(call.name, call.arguments)
                if not allowed:
                    events.append(EventType.PERMISSION_ASK, {"approved": False, "reason": denial_reason})
                    context.append(
                        Message(
                            role=Role.TOOL,
                            content=denial_reason or "Permission denied",
                            name=call.name,
                            tool_call_id=call.id,
                        )
                    )
                    continue

                hook_context = {"turn": turn, "session_id": events.session_id}
                hooked_call = await hooks.run_before_tool(call, hook_context)
                if hooked_call is None:
                    context.append(
                        Message(
                            role=Role.TOOL,
                            content="Tool call blocked by hook",
                            name=call.name,
                            tool_call_id=call.id,
                        )
                    )
                    continue
                hooked_call.id = call.id
                call = hooked_call

                events.append(
                    EventType.TOOL_CALL,
                    {"id": call.id, "name": call.name, "arguments": call.arguments},
                )
                tool_result = await self._execute_tool(call, tool_map, env, ctx, context)
                tool_result = await hooks.run_after_tool(call, tool_result, hook_context)

                events.append(
                    EventType.TOOL_RESULT,
                    {
                        "tool_call_id": call.id,
                        "name": call.name,
                        "content": tool_result.content,
                        "is_error": tool_result.is_error,
                    },
                )
                context.append(
                    Message(
                        role=Role.TOOL,
                        content=tool_result.content,
                        name=call.name,
                        tool_call_id=call.id,
                    )
                )

                failure_streak = self._record_failure_streak(
                    failure_streak, tool_result.is_error, not tool_result.is_error,
                    context, events, turn,
                )

                signature = _call_signature(call)
                if signature == last_signature:
                    repeat_count += 1
                else:
                    last_signature = signature
                    repeat_count = 1
                if repeat_count >= REPEAT_THRESHOLD:
                    context.append(
                        Message(role=Role.USER, content=REPEAT_NUDGE.format(count=repeat_count))
                    )
                    repeat_count = 0
                    last_signature = None

        events.append(EventType.SESSION_END, {"success": False, "reason": "max_turns"})
        return self._result(
            False, final_message or "Max turns exceeded", context, config.max_turns, events, usage_totals
        )

    def _record_failure_streak(
        self,
        streak: int,
        had_error: bool,
        had_success: bool,
        context: ContextManager,
        events: EventStore,
        turn: int,
    ) -> int:
        """Update the consecutive-failure streak for a tool step and steer if stuck.

        A step with any success resets the streak; an all-error step increments it.
        At the threshold a steering nudge is injected and the streak resets.
        """
        if had_success:
            return 0
        if had_error:
            streak += 1
        if streak >= FAILURE_STREAK_THRESHOLD:
            context.append(Message(role=Role.USER, content=FAILURE_STEER_NUDGE.format(count=streak)))
            events.append(
                EventType.TOOL_RESULT,
                {"failure_streak": streak, "steered": True, "turn": turn},
            )
            return 0
        return streak

    async def _run_parallel_reads(
        self,
        calls: list[ToolCall],
        tool_map: dict[str, Tool],
        env: Environment,
        ctx: ToolContext,
        context: ContextManager,
        permissions: PermissionEngine,
        hooks: HookRegistry,
        events: EventStore,
        turn: int,
    ) -> tuple[int, int]:
        """Execute a batch of read-only tool calls concurrently, preserving the
        transcript order of results and each call's tool_call_id pairing.

        Permission checks and before-hooks run sequentially (deterministic
        ordering of denials); only the side-effect-free executions are gathered.
        Returns ``(n_results, n_errors)`` for failure-streak tracking.
        """
        plan: list[tuple] = []  # ("msg", Message) | ("exec", call, hook_context)
        for call in calls:
            allowed, denial_reason = await permissions.evaluate_tool_call(call.name, call.arguments)
            if not allowed:
                events.append(EventType.PERMISSION_ASK, {"approved": False, "reason": denial_reason})
                plan.append(
                    ("msg", Message(role=Role.TOOL, content=denial_reason or "Permission denied",
                                    name=call.name, tool_call_id=call.id))
                )
                continue
            hook_context = {"turn": turn, "session_id": events.session_id}
            hooked_call = await hooks.run_before_tool(call, hook_context)
            if hooked_call is None:
                plan.append(
                    ("msg", Message(role=Role.TOOL, content="Tool call blocked by hook",
                                    name=call.name, tool_call_id=call.id))
                )
                continue
            hooked_call.id = call.id
            plan.append(("exec", hooked_call, hook_context))

        exec_indices = [i for i, entry in enumerate(plan) if entry[0] == "exec"]
        results = await asyncio.gather(
            *(self._execute_tool(plan[i][1], tool_map, env, ctx, context) for i in exec_indices)
        )
        result_by_index = dict(zip(exec_indices, results))

        n_results = 0
        n_errors = 0
        for i, entry in enumerate(plan):
            if entry[0] == "msg":
                context.append(entry[1])
                continue
            _, call, hook_context = entry
            tool_result = await hooks.run_after_tool(call, result_by_index[i], hook_context)
            n_results += 1
            if tool_result.is_error:
                n_errors += 1
            events.append(
                EventType.TOOL_CALL, {"id": call.id, "name": call.name, "arguments": call.arguments}
            )
            events.append(
                EventType.TOOL_RESULT,
                {
                    "tool_call_id": call.id,
                    "name": call.name,
                    "content": tool_result.content,
                    "is_error": tool_result.is_error,
                },
            )
            context.append(
                Message(role=Role.TOOL, content=tool_result.content, name=call.name, tool_call_id=call.id)
            )
        return n_results, n_errors

    async def _handle_task_complete(
        self,
        call: ToolCall,
        task: str,
        context: ContextManager,
        env: Environment,
        config: AgentConfig,
        events: EventStore,
        turn: int,
        usage_totals: dict[str, int] | None = None,
        permissions: PermissionEngine | None = None,
        model: Model | None = None,
    ) -> AgentResult | None:
        summary = call.arguments.get("summary", "")
        verification_commands = call.arguments.get("verification_commands") or []
        answer_rationale = call.arguments.get("answer_rationale")
        result = await self._verifier.verify_with_commands(
            task=task,
            summary=summary,
            verification_commands=verification_commands,
            env=env,
            config=config,
            permissions=permissions,
            model=model if config.enable_llm_verifier else None,
            messages=context.get_messages(),
            answer_rationale=answer_rationale,
        )
        events.append(
            EventType.VERIFICATION,
            {"approved": result.approved, "checklist": result.checklist, "feedback": result.feedback},
        )
        if result.approved:
            events.append(EventType.SESSION_END, {"success": True, "turns": turn})
            return self._result(True, summary, context, turn, events, usage_totals)

        feedback = result.feedback or "Completion verification failed."
        context.append(
            Message(
                role=Role.TOOL,
                content=feedback,
                name="task_complete",
                tool_call_id=call.id,
            )
        )
        return None

    async def _execute_tool(
        self,
        call: ToolCall,
        tool_map: dict[str, Tool],
        env: Environment,
        ctx: ToolContext,
        context: ContextManager,
    ) -> ToolResult:
        tool = tool_map.get(call.name)
        if tool is None:
            return ToolResult(
                tool_call_id=call.id,
                content=f"Unknown tool: {call.name}",
                is_error=True,
            )
        try:
            result = await tool.execute(call.arguments, env, ctx)
            content = result.content if isinstance(result.content, str) else str(result.content)
            result.content = self._shape_or_buffer(content, call, ctx, context, result.is_error)
        except Exception as exc:
            logger.warning("Tool %s raised %s: %s", call.name, type(exc).__name__, exc)
            result = ToolResult(
                tool_call_id=call.id,
                content=context.shape_observation(
                    f"Tool '{call.name}' failed: {type(exc).__name__}: {exc}", is_error=True
                ),
                is_error=True,
            )
        result.tool_call_id = call.id
        return result

    def _shape_or_buffer(self, content, call, ctx, context, is_error):
        """Large output → store full body in the buffer + return a stub; else shape inline.

        Never lets a buffer failure crash the turn — falls back to head/tail shaping.
        """
        buffer = getattr(ctx, "buffer", None)
        if buffer is not None and content and buffer.exceeds(content):
            try:
                import hashlib

                from garuda.core.buffer import format_buffer_stub

                # Short, provider-agnostic id (some providers' tool_call ids are ~1KB).
                buffer_id = "buf_" + hashlib.sha1(call.id.encode("utf-8")).hexdigest()[:10]
                ref = buffer.store(buffer_id, content, tool_name=call.name, is_error=is_error)
                return format_buffer_stub(ref)
            except Exception as exc:
                logger.warning("Buffer store failed for %s: %s; falling back to truncation", call.name, exc)
        return context.shape_observation(content, is_error=is_error)

    def _result(
        self,
        success: bool,
        final_message: str,
        context: ContextManager,
        turns: int,
        events: EventStore,
        usage_totals: dict[str, int] | None = None,
    ) -> AgentResult:
        return AgentResult(
            success=success,
            final_message=final_message,
            messages=context.get_messages(),
            turns=turns,
            metadata={
                "session_id": events.session_id,
                "events": events.get_all(),
                "usage": dict(usage_totals or {}),
            },
        )
