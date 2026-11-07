import json
import math
from collections.abc import AsyncIterator
from dataclasses import dataclass

from garuda.model.protocol import ModelResponse, StreamDelta
from garuda.types import Message


@dataclass
class ScriptedStream:
    """An exact stream for one ``stream()`` call: deltas yielded in order, and any
    exception in the list raised at that point (a mid-stream failure). A list with
    no ``done`` delta is a stream that ends without its terminal chunk."""

    deltas: list


class ScriptModel:
    """Deterministic model for tests — returns queued responses in order.

    ``complete`` and ``stream`` take turns from one queue, so a stream that fails
    and the fallback ``complete`` it causes are scripted as consecutive entries.
    """

    def __init__(self, responses: list[ModelResponse | ScriptedStream], model_name: str = "script/test"):
        self._responses = list(responses)
        self._model_name = model_name

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
        if not self._responses:
            return ModelResponse(content="Done.", tool_calls=[])
        response = self._responses.pop(0)
        if isinstance(response, ScriptedStream):
            raise TypeError("a ScriptedStream was queued for a non-streaming call")
        return response

    async def stream(
        self,
        messages: list[Message],
        tools: list[dict] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[StreamDelta]:
        """Yield the next queued response's reasoning and content in a few chunks,
        its tool calls as trailing fragments, its usage, then done.

        Deterministic so tests can exercise the streaming path without a network.
        A queued ``ScriptedStream`` is replayed exactly instead.
        """
        response = self._responses.pop(0) if self._responses else ModelResponse(
            content="Done.", tool_calls=[]
        )
        if isinstance(response, ScriptedStream):
            for item in response.deltas:
                if isinstance(item, BaseException):
                    raise item
                yield item
            return
        for field, text in (("reasoning_delta", response.reasoning_content or ""),
                            ("content_delta", response.content or "")):
            size = max(1, math.ceil(len(text) / 3))
            for i in range(0, len(text), size):
                yield StreamDelta(**{field: text[i : i + size]})
        if response.thinking_blocks:
            yield StreamDelta(thinking_blocks=response.thinking_blocks)
        for index, call in enumerate(response.tool_calls or []):
            yield StreamDelta(
                tool_call_delta={
                    "index": index,
                    "id": call.id,
                    "name": call.name,
                    "arguments": json.dumps(call.arguments),
                }
            )
        if response.usage or response.attempt_usage:
            yield StreamDelta(usage=response.usage, attempt_usage=response.attempt_usage or None)
        yield StreamDelta(
            done=True,
            finish_reason=response.raw.get("finish_reason"),
            status=response.raw.get("status"),
        )

    def count_tokens(self, messages: list[Message]) -> int:
        return sum(len(m.content) for m in messages) // 4
