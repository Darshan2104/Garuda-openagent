import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from garuda.types import Message

# Per-tool framing the provider adds around each schema (name/description wrapper,
# separators). Small, but 25 tools' worth of it is not nothing.
TOOL_FRAMING_TOKENS = 8

# The model a run uses when nothing names one. Routed through OpenRouter on purpose:
# `LitellmModel._apply_usage_accounting` asks OpenRouter to return what each call
# actually cost, so a default run's spend is the provider's own figure rather than a
# public pricing table's guess about it — and `eval/pricing.py` deliberately has no
# entry for this model, because an invented rate is worse than none.
#
# One constant, not a literal per entry point. This string was copied into seven
# places (four CLI subcommands, the JSON-RPC server, and both SDK classes), which is
# the shape of every literal-drift bug this repo has already had to pin with a test
# — `tests/test_model_defaults.py` now asserts they all agree.
DEFAULT_MODEL = "openrouter/deepseek/deepseek-v4-flash-0731"

# The env var that overrides it, read at parser-build time by every subcommand.
MODEL_ENV_VAR = "GARUDA_MODEL"


class ContextOverflowError(Exception):
    """The prompt did not fit the model's context window.

    Provider-agnostic on purpose, so the loop can recognise the one model failure
    it can actually do something about without importing a provider's exception
    hierarchy. Retrying the same request is futile — the fix is to make the prompt
    smaller first, which is the caller's job, not the client's.
    """


@dataclass
class ModelResponse:
    content: str | None
    tool_calls: list[Any]
    raw: dict[str, Any] = field(default_factory=dict)
    usage: dict[str, int] = field(default_factory=dict)
    # Extended-thinking output. ``reasoning_content`` is the human-readable
    # reasoning text; ``thinking_blocks`` are the provider's structured blocks
    # (with signatures) that must be echoed back on the next request to preserve
    # interleaved thinking across tool-call turns (Anthropic).
    reasoning_content: str | None = None
    thinking_blocks: list[dict] | None = None


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
    reasoning_delta: str = ""
    thinking_blocks: list[dict] | None = None
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

    # Optional. Counts the *whole request* — messages plus the tool schemas that
    # ride along with every call. Implementations MAY provide it; callers must go
    # through the ``count_request_tokens`` free function below, which falls back to
    # ``count_tokens`` plus a local schema estimate.
    def count_request_tokens(
        self, messages: list[Message], tools: list[dict] | None = None
    ) -> int: ...

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


def estimate_tools_tokens(tools: list[dict] | None) -> int:
    """Local estimate of what a tool-schema list costs in the prompt.

    Tool schemas are sent on every single call and are the largest fixed cost in
    an agentic request — a 25-tool toolkit is routinely 5-10k tokens. A budget
    that counts only messages is wrong by that much on every turn.
    """
    if not tools:
        return 0
    try:
        serialized = json.dumps(tools, default=str)
    except (TypeError, ValueError):
        serialized = str(tools)
    return len(serialized) // 4 + TOOL_FRAMING_TOKENS * len(tools)


def count_request_tokens(
    model: object, messages: list[Message], tools: list[dict] | None = None
) -> int:
    """Best available count of the complete next request.

    Prefers a model-native counter that can see the tool schemas; otherwise falls
    back to the message-only ``count_tokens`` plus a local schema estimate. The
    fallback is what keeps every existing ``Model`` implementation working without
    growing a new method.
    """
    native = getattr(model, "count_request_tokens", None)
    if native is not None:
        return native(messages, tools)
    return model.count_tokens(messages) + estimate_tools_tokens(tools)
