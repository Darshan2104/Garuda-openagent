import json
import uuid

from garuda.core.events import EventStore, EventType
from garuda.model.protocol import Model
from garuda.tools.protocol import Tool, ToolContext
from garuda.types import (
    DEFAULT_SYSTEM_PROMPT,
    AgentConfig,
    AgentResult,
    Message,
    Role,
    ToolCall,
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


def _shape_output(text: str, max_bytes: int) -> str:
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    half = max_bytes // 2
    head = encoded[:half].decode("utf-8", errors="ignore")
    tail = encoded[-half:].decode("utf-8", errors="ignore")
    return f"{head}\n\n...[truncated]...\n\n{tail}"


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
    ) -> AgentResult:
        config = config or AgentConfig()
        events = events or EventStore()
        events.append(EventType.SESSION_START, {"task": task, "model": model.model_name})

        tool_map = {tool.name: tool for tool in tools}
        if config.allowed_tools:
            tool_map = {name: tool_map[name] for name in config.allowed_tools if name in tool_map}
            tools = list(tool_map.values())

        system_prompt = config.system_prompt or DEFAULT_SYSTEM_PROMPT
        messages: list[Message] = [
            Message(role=Role.SYSTEM, content=system_prompt),
            Message(role=Role.USER, content=task),
        ]
        events.append(EventType.USER_MESSAGE, {"content": task})

        ctx = ToolContext(session_id=events.session_id, agent_profile=self._profile_name)
        final_message = ""

        for turn in range(1, config.max_turns + 1):
            response = await model.complete(messages, tools=_tools_schema(tools))
            events.append(
                EventType.MODEL_RESPONSE,
                {
                    "content": response.content,
                    "tool_calls": [
                        {"name": c.name, "arguments": c.arguments} for c in response.tool_calls
                    ],
                },
            )

            if response.content:
                messages.append(Message(role=Role.ASSISTANT, content=response.content))

            if not response.tool_calls:
                final_message = response.content or ""
                if self._is_completion_message(final_message):
                    events.append(EventType.SESSION_END, {"success": True, "turns": turn})
                    return AgentResult(
                        success=True,
                        final_message=final_message,
                        messages=messages,
                        turns=turn,
                        metadata={"session_id": events.session_id, "events": events.get_all()},
                    )
                continue

            for call in response.tool_calls:
                tool_result = await self._execute_tool(call, tool_map, env, ctx, config.max_output_bytes)
                events.append(
                    EventType.TOOL_CALL,
                    {"name": call.name, "arguments": call.arguments},
                )
                events.append(
                    EventType.TOOL_RESULT,
                    {"name": call.name, "content": tool_result.content, "is_error": tool_result.is_error},
                )
                messages.append(
                    Message(
                        role=Role.TOOL,
                        content=tool_result.content,
                        name=call.name,
                        tool_call_id=call.id,
                    )
                )

        events.append(EventType.SESSION_END, {"success": False, "reason": "max_turns"})
        return AgentResult(
            success=False,
            final_message=final_message or "Max turns exceeded",
            messages=messages,
            turns=config.max_turns,
            metadata={"session_id": events.session_id, "events": events.get_all()},
        )

    async def _execute_tool(
        self,
        call: ToolCall,
        tool_map: dict[str, Tool],
        env: Environment,
        ctx: ToolContext,
        max_output_bytes: int,
    ):
        from garuda.types import ToolResult

        tool = tool_map.get(call.name)
        if tool is None:
            return ToolResult(
                tool_call_id=call.id,
                content=f"Unknown tool: {call.name}",
                is_error=True,
            )
        result = await tool.execute(call.arguments, env, ctx)
        result.tool_call_id = call.id
        result.content = _shape_output(result.content, max_output_bytes)
        return result

    def _is_completion_message(self, text: str) -> bool:
        lowered = text.lower()
        markers = ("task complete", "done.", "finished", "completed the task")
        return any(marker in lowered for marker in markers)
