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

CONTEXT_WARNING_FRACTION = 0.8


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
            )

        ctx = ToolContext(
            session_id=events.session_id,
            agent_profile=self._profile_name,
            model=model,
            subagent_runner=subagent_runner,
        )
        final_message = ""
        usage_totals: dict[str, int] = {}
        last_signature: str | None = None
        repeat_count = 0
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
            events.append(
                EventType.MODEL_RESPONSE,
                {
                    "content": response.content,
                    "tool_calls": [
                        {"id": call.id, "name": call.name, "arguments": call.arguments}
                        for call in response.tool_calls
                    ],
                    "usage": response.usage,
                },
            )

            if response.content or response.tool_calls:
                context.append(
                    Message(
                        role=Role.ASSISTANT,
                        content=response.content or "",
                        tool_calls=list(response.tool_calls) or None,
                    )
                )

            if not response.tool_calls:
                final_message = response.content or ""
                if not config.enable_verifier:
                    events.append(EventType.SESSION_END, {"success": True, "turns": turn})
                    return self._result(True, final_message, context, turn, events, usage_totals)
                context.append(Message(role=Role.USER, content=CONTINUE_NUDGE))
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
        result = await self._verifier.verify_with_commands(
            task=task,
            summary=summary,
            verification_commands=verification_commands,
            env=env,
            config=config,
            permissions=permissions,
            model=model if config.enable_llm_verifier else None,
            messages=context.get_messages(),
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
        except Exception as exc:
            logger.warning("Tool %s raised %s: %s", call.name, type(exc).__name__, exc)
            result = ToolResult(
                tool_call_id=call.id,
                content=f"Tool '{call.name}' failed: {type(exc).__name__}: {exc}",
                is_error=True,
            )
        result.tool_call_id = call.id
        result.content = context.shape_observation(result.content, is_error=result.is_error)
        return result

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
