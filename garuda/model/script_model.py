import json
import math
from collections.abc import AsyncIterator

from garuda.model.protocol import ModelResponse, StreamDelta
from garuda.types import Message


class ScriptModel:
    """Deterministic model for tests — returns queued responses in order."""

    def __init__(self, responses: list[ModelResponse], model_name: str = "script/test"):
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
        return self._responses.pop(0)

    async def stream(
        self,
        messages: list[Message],
        tools: list[dict] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[StreamDelta]:
        """Yield the next queued response's content in a few chunks, then done.

        Deterministic so tests can exercise the streaming path without a network.
        Any tool calls on the response are emitted as trailing fragments.
        """
        response = self._responses.pop(0) if self._responses else ModelResponse(
            content="Done.", tool_calls=[]
        )
        content = response.content or ""
        if content:
            size = max(1, math.ceil(len(content) / 3))
            for i in range(0, len(content), size):
                yield StreamDelta(content_delta=content[i : i + size])
        for index, call in enumerate(response.tool_calls or []):
            yield StreamDelta(
                tool_call_delta={
                    "index": index,
                    "id": call.id,
                    "name": call.name,
                    "arguments": json.dumps(call.arguments),
                }
            )
        yield StreamDelta(done=True)

    def count_tokens(self, messages: list[Message]) -> int:
        return sum(len(m.content) for m in messages) // 4
