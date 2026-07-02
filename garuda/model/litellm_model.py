import asyncio
import inspect
import json
import logging
import uuid
from collections.abc import AsyncIterator

import litellm

from garuda.model.protocol import ModelResponse, StreamDelta
from garuda.types import Message, Role, ToolCall

logger = logging.getLogger(__name__)

TOOL_ARG_PARSE_ERROR_KEY = "__tool_arg_parse_error__"

_RETRYABLE_EXCEPTIONS = (
    litellm.RateLimitError,
    litellm.APIConnectionError,
    litellm.InternalServerError,
    litellm.ServiceUnavailableError,
    litellm.Timeout,
)


def _serialize_tool_calls(tool_calls: list[ToolCall]) -> list[dict]:
    return [
        {
            "id": call.id,
            "type": "function",
            "function": {
                "name": call.name,
                "arguments": json.dumps(call.arguments),
            },
        }
        for call in tool_calls
    ]


def _message_to_litellm(message: Message) -> dict:
    if message.role == Role.ASSISTANT:
        payload: dict = {"role": "assistant", "content": message.content or None}
        if message.tool_calls:
            payload["tool_calls"] = _serialize_tool_calls(message.tool_calls)
        return payload
    if message.role == Role.TOOL:
        return {
            "role": "tool",
            "tool_call_id": message.tool_call_id or "",
            "content": message.content,
        }
    return {"role": message.role.value, "content": message.content}


def _parse_tool_calls(raw_calls: list) -> list[ToolCall]:
    parsed: list[ToolCall] = []
    for call in raw_calls:
        fn = call.function if hasattr(call, "function") else call.get("function", {})
        name = fn.name if hasattr(fn, "name") else fn.get("name", "")
        raw_args = fn.arguments if hasattr(fn, "arguments") else fn.get("arguments", "{}")
        if isinstance(raw_args, str):
            try:
                arguments = json.loads(raw_args) if raw_args.strip() else {}
            except json.JSONDecodeError as exc:
                arguments = {TOOL_ARG_PARSE_ERROR_KEY: f"Malformed tool arguments ({exc}): {raw_args[:2000]}"}
        else:
            arguments = raw_args or {}
        if not isinstance(arguments, dict):
            arguments = {TOOL_ARG_PARSE_ERROR_KEY: f"Tool arguments must be an object, got: {str(arguments)[:2000]}"}
        call_id = call.id if hasattr(call, "id") else call.get("id", str(uuid.uuid4()))
        parsed.append(ToolCall(id=call_id, name=name, arguments=arguments))
    return parsed


def _stream_deltas_from_chunk(chunk) -> list[StreamDelta]:
    """Translate one litellm streaming chunk into zero or more StreamDeltas."""
    deltas: list[StreamDelta] = []
    choices = getattr(chunk, "choices", None) or []
    if not choices:
        return deltas
    delta = getattr(choices[0], "delta", None)
    if delta is None:
        return deltas
    content = getattr(delta, "content", None)
    if content:
        deltas.append(StreamDelta(content_delta=content))
    for tc in getattr(delta, "tool_calls", None) or []:
        fn = getattr(tc, "function", None)
        deltas.append(
            StreamDelta(
                tool_call_delta={
                    "index": getattr(tc, "index", 0) or 0,
                    "id": getattr(tc, "id", None),
                    "name": getattr(fn, "name", None) if fn is not None else None,
                    "arguments": getattr(fn, "arguments", None) if fn is not None else None,
                }
            )
        )
    return deltas


def _merge_tool_fragment(tool_frags: dict[int, dict], frag: dict) -> None:
    """Fold a streamed tool-call fragment into per-index accumulators."""
    index = frag.get("index", 0) or 0
    slot = tool_frags.setdefault(index, {"id": None, "name": None, "arguments": ""})
    if frag.get("id"):
        slot["id"] = frag["id"]
    if frag.get("name"):
        slot["name"] = frag["name"]
    if frag.get("arguments"):
        slot["arguments"] += frag["arguments"]


def _extract_usage(response) -> dict[str, int]:
    usage: dict[str, int] = {}
    if not getattr(response, "usage", None):
        return usage
    raw = response.usage
    usage["prompt_tokens"] = raw.prompt_tokens or 0
    usage["completion_tokens"] = raw.completion_tokens or 0
    usage["total_tokens"] = getattr(raw, "total_tokens", None) or (
        usage["prompt_tokens"] + usage["completion_tokens"]
    )
    details = getattr(raw, "prompt_tokens_details", None)
    cached = getattr(details, "cached_tokens", None) if details else None
    if cached:
        usage["cache_read_tokens"] = cached
    cache_creation = getattr(raw, "cache_creation_input_tokens", None)
    if cache_creation:
        usage["cache_creation_tokens"] = cache_creation
    return usage


class LitellmModel:
    def __init__(
        self,
        model_name: str,
        api_key: str | None = None,
        api_base: str | None = None,
        max_retries: int = 3,
        request_timeout: float = 600.0,
        enable_prompt_caching: bool = True,
    ):
        self._model_name = model_name
        self._api_key = api_key
        self._api_base = api_base
        self._max_retries = max_retries
        self._request_timeout = request_timeout
        self._enable_prompt_caching = enable_prompt_caching

    @property
    def model_name(self) -> str:
        return self._model_name

    @property
    def supports_tool_calling(self) -> bool:
        return True

    def _supports_cache_control(self) -> bool:
        if not self._enable_prompt_caching:
            return False
        name = self._model_name.lower()
        return name.startswith("anthropic/") or "claude" in name

    def _apply_cache_control(self, messages: list[dict]) -> list[dict]:
        """Mark the system prompt and the last message as Anthropic cache breakpoints.

        The system breakpoint caches the stable prefix; the moving last-message
        breakpoint caches the growing conversation incrementally.
        """

        def mark(payload: dict) -> None:
            content = payload.get("content")
            if isinstance(content, str) and content:
                payload["content"] = [
                    {"type": "text", "text": content, "cache_control": {"type": "ephemeral"}}
                ]

        if messages and messages[0]["role"] == "system":
            mark(messages[0])
        for payload in reversed(messages):
            if payload.get("content") and payload["role"] != "system":
                mark(payload)
                break
        return messages

    def _build_kwargs(
        self,
        messages: list[Message],
        tools: list[dict] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> dict:
        """Build the litellm request kwargs shared by ``complete`` and ``stream``.

        This is the single source of truth for message serialization,
        cache-control breakpoints, auth, tool wiring, and sampling params so the
        blocking and streaming paths never drift apart.
        """
        litellm_messages = [_message_to_litellm(m) for m in messages]
        if self._supports_cache_control():
            litellm_messages = self._apply_cache_control(litellm_messages)
        kwargs: dict = {
            "model": self._model_name,
            "messages": litellm_messages,
            "timeout": self._request_timeout,
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
        return kwargs

    async def complete(
        self,
        messages: list[Message],
        tools: list[dict] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> ModelResponse:
        kwargs = self._build_kwargs(messages, tools, temperature, max_tokens)

        response = await self._complete_with_retries(kwargs)
        choice = response.choices[0]
        message = choice.message
        content = message.content
        tool_calls = _parse_tool_calls(message.tool_calls or [])

        return ModelResponse(
            content=content,
            tool_calls=tool_calls,
            raw={
                "model": self._model_name,
                "finish_reason": getattr(choice, "finish_reason", None),
            },
            usage=_extract_usage(response),
        )

    async def _complete_with_retries(self, kwargs: dict):
        delay = 1.0
        last_exc: Exception | None = None
        for attempt in range(1, self._max_retries + 1):
            try:
                return await litellm.acompletion(**kwargs)
            except _RETRYABLE_EXCEPTIONS as exc:
                last_exc = exc
                if attempt == self._max_retries:
                    break
                logger.warning(
                    "Model call failed (%s), retry %d/%d in %.1fs",
                    type(exc).__name__,
                    attempt,
                    self._max_retries - 1,
                    delay,
                )
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30.0)
        raise last_exc  # type: ignore[misc]

    async def stream(
        self,
        messages: list[Message],
        tools: list[dict] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[StreamDelta]:
        """Yield incremental deltas from a streaming completion.

        Establishing the stream (the initial ``acompletion`` call) is retried via
        the same backoff as ``complete``; once the first byte flows, iteration
        errors are surfaced to the caller rather than retried, since a partial
        response cannot be safely restarted.
        """
        kwargs = self._build_kwargs(messages, tools, temperature, max_tokens)
        kwargs["stream"] = True
        response_stream = await self._complete_with_retries(kwargs)
        async for chunk in response_stream:
            for delta in _stream_deltas_from_chunk(chunk):
                yield delta
        yield StreamDelta(done=True)

    async def complete_streaming(
        self,
        messages: list[Message],
        tools: list[dict] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        on_delta=None,
    ) -> ModelResponse:
        """Consume ``stream`` into a full ModelResponse.

        Accumulates content and tool-call fragments while invoking the optional
        ``on_delta(text)`` callback (sync or async) for each text chunk, so a
        caller can render tokens live and still receive the assembled response.
        """
        content_parts: list[str] = []
        tool_frags: dict[int, dict] = {}
        async for delta in self.stream(messages, tools, temperature, max_tokens):
            if delta.content_delta:
                content_parts.append(delta.content_delta)
                if on_delta is not None:
                    result = on_delta(delta.content_delta)
                    if inspect.isawaitable(result):
                        await result
            if delta.tool_call_delta:
                _merge_tool_fragment(tool_frags, delta.tool_call_delta)

        content = "".join(content_parts) or None
        raw_calls = [
            {
                "id": slot["id"] or str(uuid.uuid4()),
                "function": {"name": slot["name"] or "", "arguments": slot["arguments"] or "{}"},
            }
            for _, slot in sorted(tool_frags.items())
        ]
        return ModelResponse(
            content=content,
            tool_calls=_parse_tool_calls(raw_calls),
            raw={"model": self._model_name, "streamed": True},
            usage={},
        )

    def count_tokens(self, messages: list[Message]) -> int:
        try:
            return litellm.token_counter(
                model=self._model_name,
                messages=[_message_to_litellm(m) for m in messages],
            )
        except Exception:
            text = "\n".join(m.content or "" for m in messages)
            return len(text) // 4
