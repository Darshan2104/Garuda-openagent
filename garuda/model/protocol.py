from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from garuda.types import Message


@dataclass
class ModelResponse:
    content: str | None
    tool_calls: list[Any]
    raw: dict[str, Any] = field(default_factory=dict)
    usage: dict[str, int] = field(default_factory=dict)


@runtime_checkable
class Model(Protocol):
    @property
    def model_name(self) -> str: ...

    @property
    def supports_tool_calling(self) -> bool: ...

    async def complete(
        self,
        messages: list[Message],
        tools: list[dict] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> ModelResponse: ...

    def count_tokens(self, messages: list[Message]) -> int: ...
