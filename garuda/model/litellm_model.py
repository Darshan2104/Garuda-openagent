import json
import re
import uuid

import litellm

from garuda.model.protocol import ModelResponse
from garuda.types import Message, Role, ToolCall


def _message_to_litellm(message: Message) -> dict:
    payload: dict = {"role": message.role.value, "content": message.content}
    if message.name:
        payload["name"] = message.name
    if message.tool_call_id:
        payload["tool_call_id"] = message.tool_call_id
    return payload


def _parse_tool_calls(raw_calls: list) -> list[ToolCall]:
    parsed: list[ToolCall] = []
    for call in raw_calls:
        fn = call.function if hasattr(call, "function") else call.get("function", {})
        name = fn.name if hasattr(fn, "name") else fn.get("name", "")
        raw_args = fn.arguments if hasattr(fn, "arguments") else fn.get("arguments", "{}")
        arguments = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
        call_id = call.id if hasattr(call, "id") else call.get("id", str(uuid.uuid4()))
        parsed.append(ToolCall(id=call_id, name=name, arguments=arguments))
    return parsed


def parse_bash_blocks(text: str) -> list[ToolCall]:
    """Extract bash commands from markdown fences for non-tool-calling models."""
    pattern = re.compile(r"```(?:bash|sh|shell)?\s*\n(.*?)```", re.DOTALL | re.IGNORECASE)
    calls: list[ToolCall] = []
    for index, match in enumerate(pattern.finditer(text)):
        command = match.group(1).strip()
        if command:
            calls.append(
                ToolCall(
                    id=f"bash-{index}",
                    name="bash",
                    arguments={"command": command},
                )
            )
    return calls


class LitellmModel:
    def __init__(self, model_name: str, api_key: str | None = None, api_base: str | None = None):
        self._model_name = model_name
        self._api_key = api_key
        self._api_base = api_base

    @property
    def model_name(self) -> str:
        return self._model_name

    @property
    def supports_tool_calling(self) -> bool:
        return True

    async def complete(
        self,
        messages: list[Message],
        tools: list[dict] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> ModelResponse:
        kwargs: dict = {
            "model": self._model_name,
            "messages": [_message_to_litellm(m) for m in messages],
        }
        if self._api_key:
            kwargs["api_key"] = self._api_key
        if self._api_base:
            kwargs["api_base"] = self._api_base
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"
        if temperature is not None:
            kwargs["temperature"] = temperature
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens

        response = await litellm.acompletion(**kwargs)
        choice = response.choices[0]
        message = choice.message
        content = message.content
        raw_tool_calls = message.tool_calls or []
        tool_calls = _parse_tool_calls(raw_tool_calls)

        if not tool_calls and content:
            tool_calls = parse_bash_blocks(content)

        usage = {}
        if response.usage:
            usage = {
                "prompt_tokens": response.usage.prompt_tokens or 0,
                "completion_tokens": response.usage.completion_tokens or 0,
            }

        return ModelResponse(
            content=content,
            tool_calls=tool_calls,
            raw={"model": self._model_name},
            usage=usage,
        )

    def count_tokens(self, messages: list[Message]) -> int:
        text = "\n".join(m.content for m in messages)
        return len(text) // 4
