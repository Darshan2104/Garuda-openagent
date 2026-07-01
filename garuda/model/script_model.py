from garuda.model.protocol import ModelResponse
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

    def count_tokens(self, messages: list[Message]) -> int:
        return sum(len(m.content) for m in messages) // 4
