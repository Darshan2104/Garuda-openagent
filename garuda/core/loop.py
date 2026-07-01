from garuda.context.manager import ContextManager
from garuda.core.events import EventStore, EventType
from garuda.core.permissions import PermissionEngine
from garuda.core.verifier import CompletionVerifier
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

        system_prompt = config.system_prompt or DEFAULT_SYSTEM_PROMPT
        context = ContextManager(
            model=model,
            max_output_bytes=config.max_output_bytes,
            proactive_threshold=config.proactive_summarize_threshold,
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
                permissions=permissions,
                events=events,
            )

        ctx = ToolContext(
            session_id=events.session_id,
            agent_profile=self._profile_name,
            model=model,
            subagent_runner=subagent_runner,
        )
        final_message = ""

        for turn in range(1, config.max_turns + 1):
            if await context.maybe_summarize():
                events.append(EventType.SUMMARIZATION, {"turn": turn})

            response = await model.complete(
                context.get_messages(),
                tools=_tools_schema(tools),
            )
            events.append(
                EventType.MODEL_RESPONSE,
                {
                    "content": response.content,
                    "tool_calls": [
                        {"name": call.name, "arguments": call.arguments}
                        for call in response.tool_calls
                    ],
                },
            )

            if response.content:
                context.append(Message(role=Role.ASSISTANT, content=response.content))

            if not response.tool_calls:
                final_message = response.content or ""
                if not config.enable_verifier and self._is_completion_message(final_message):
                    events.append(EventType.SESSION_END, {"success": True, "turns": turn})
                    return self._result(True, final_message, context, turn, events)
                continue

            for call in response.tool_calls:
                if call.name == "task_complete":
                    completed = await self._handle_task_complete(
                        call, task, context, env, config, events, turn
                    )
                    if completed is not None:
                        return completed
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
                call = await hooks.run_before_tool(call, hook_context)
                if call is None:
                    context.append(
                        Message(
                            role=Role.TOOL,
                            content="Tool call blocked by hook",
                            name="hook",
                            tool_call_id="blocked",
                        )
                    )
                    continue

                tool_result = await self._execute_tool(call, tool_map, env, ctx, context)
                tool_result = await hooks.run_after_tool(call, tool_result, hook_context)

                events.append(EventType.TOOL_CALL, {"name": call.name, "arguments": call.arguments})
                events.append(
                    EventType.TOOL_RESULT,
                    {
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

        events.append(EventType.SESSION_END, {"success": False, "reason": "max_turns"})
        return self._result(False, final_message or "Max turns exceeded", context, config.max_turns, events)

    async def _handle_task_complete(
        self,
        call: ToolCall,
        task: str,
        context: ContextManager,
        env: Environment,
        config: AgentConfig,
        events: EventStore,
        turn: int,
    ) -> AgentResult | None:
        summary = call.arguments.get("summary", "")
        verification_commands = call.arguments.get("verification_commands") or []
        result = await self._verifier.verify_with_commands(
            task=task,
            summary=summary,
            verification_commands=verification_commands,
            env=env,
            config=config,
        )
        events.append(
            EventType.VERIFICATION,
            {"approved": result.approved, "checklist": result.checklist, "feedback": result.feedback},
        )
        if result.approved:
            events.append(EventType.SESSION_END, {"success": True, "turns": turn})
            return self._result(True, summary, context, turn, events)

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
        result = await tool.execute(call.arguments, env, ctx)
        result.tool_call_id = call.id
        result.content = context.shape_observation(result.content)
        return result

    def _result(
        self,
        success: bool,
        final_message: str,
        context: ContextManager,
        turns: int,
        events: EventStore,
    ) -> AgentResult:
        return AgentResult(
            success=success,
            final_message=final_message,
            messages=context.get_messages(),
            turns=turns,
            metadata={"session_id": events.session_id, "events": events.get_all()},
        )

    def _is_completion_message(self, text: str) -> bool:
        lowered = text.lower()
        markers = ("task complete", "done.", "finished", "completed the task")
        return any(marker in lowered for marker in markers)
