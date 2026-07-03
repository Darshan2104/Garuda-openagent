from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from garuda.types import Message


@dataclass
class ModelResponse:
    content: str | None
    tool_calls: list[Any]
    raw: dict[str, Any] = field(default_factory=dict)
    usage: dict[str, int] = field(default_factory=dict)


@dataclass
class StreamDelta:
    """One incremental chunk from a streaming model call.

    ``content_delta`` carries newly generated assistant text; ``tool_call_delta``
    carries a partial tool-call fragment (with, e.g., ``index``, ``id``, ``name``,
    ``arguments`` keys as they arrive); ``done`` marks the terminal chunk.
    """

    content_delta: str = ""
    tool_call_delta: dict | None = None
    usage: dict[str, int] | None = None
    done: bool = False


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

    # Optional streaming interface. Implementations MAY provide ``stream`` to
    # yield incremental deltas; callers must gate on ``supports_streaming`` since
    # not every Model implements it.
    def stream(
        self,
        messages: list[Message],
        tools: list[dict] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[StreamDelta]: ...


def supports_streaming(model: object) -> bool:
    """True when ``model`` exposes the optional streaming interface."""
    return hasattr(model, "stream")
