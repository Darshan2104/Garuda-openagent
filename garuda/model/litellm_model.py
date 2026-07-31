import asyncio
import email.utils
import inspect
import json
import logging
import random
import time
import uuid
from collections.abc import AsyncIterator

import litellm

from garuda.model.governor import get_governor, provider_of
from garuda.model.protocol import ModelResponse, StreamDelta
from garuda.types import Message, Role, ToolCall

logger = logging.getLogger(__name__)

TOOL_ARG_PARSE_ERROR_KEY = "__tool_arg_parse_error__"

# Exception classes that are always transient and worth retrying.
_RETRYABLE_EXCEPTION_TYPES = (
    litellm.RateLimitError,
    litellm.APIConnectionError,
    litellm.InternalServerError,
    litellm.ServiceUnavailableError,
    litellm.Timeout,
)

# HTTP statuses that are transient even when litellm maps them to a generic
# APIError (e.g. Cloudflare 520–524, overloaded 529). 4xx like 400/401/403/404
# and ContextWindowExceeded are deliberately excluded — retrying them is futile.
_RETRYABLE_STATUS_CODES = frozenset({408, 409, 425, 429, 500, 502, 503, 504, 520, 521, 522, 523, 524, 529})

# Upper bound on any single backoff/Retry-After sleep.
_MAX_RETRY_SLEEP = 60.0


def _is_retryable(exc: Exception) -> bool:
    """True if the exception is a transient failure worth retrying."""
    if isinstance(exc, _RETRYABLE_EXCEPTION_TYPES):
        return True
    status = getattr(exc, "status_code", None)
    return isinstance(status, int) and status in _RETRYABLE_STATUS_CODES


def _retry_after_seconds(exc: Exception) -> float | None:
    """Extract a server-provided Retry-After (seconds or HTTP-date), capped."""
    val = getattr(exc, "retry_after", None)
    if isinstance(val, (int, float)) and val > 0:
        return min(float(val), _MAX_RETRY_SLEEP)
    headers = getattr(getattr(exc, "response", None), "headers", None)
    raw = None
    if headers is not None:
        try:
            raw = headers.get("retry-after") or headers.get("Retry-After")
        except Exception:
            raw = None
    if raw is None:
        return None
    try:
        return min(float(raw), _MAX_RETRY_SLEEP)
    except (TypeError, ValueError):
        try:
            when = email.utils.parsedate_to_datetime(str(raw))
            secs = when.timestamp() - time.time()
            return min(secs, _MAX_RETRY_SLEEP) if secs > 0 else None
        except Exception:
            return None


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


def _normalize_thinking_blocks(blocks) -> list[dict] | None:
    """Coerce litellm thinking blocks (pydantic models or dicts) into plain dicts.

    Plain dicts are JSON-serializable (for events/sessions) and can be echoed back
    verbatim on the next request to preserve interleaved thinking.
    """
    if not blocks:
        return None
    normalized: list[dict] = []
    for block in blocks:
        if isinstance(block, dict):
            normalized.append(block)
        elif hasattr(block, "model_dump"):
            normalized.append(block.model_dump(exclude_none=True))
        elif hasattr(block, "__dict__"):
            normalized.append({k: v for k, v in vars(block).items() if v is not None})
    return normalized or None


def _with_images(content: str, images: list[str]) -> list[dict]:
    """Build a content-block list: the text plus one image_url block per image."""
    blocks: list[dict] = [{"type": "text", "text": content or ""}]
    for uri in images:
        blocks.append({"type": "image_url", "image_url": {"url": uri}})
    return blocks


def _message_to_litellm(
    message: Message,
    include_thinking: bool = False,
    include_images: bool = False,
    include_reasoning: bool = False,
) -> dict:
    # A message carrying images renders as a multimodal content-block list (user
    # role only — portable across OpenAI/Anthropic; tool-role images aren't).
    if include_images and message.images and message.role in (Role.USER, Role.SYSTEM):
        return {"role": message.role.value, "content": _with_images(message.content, message.images)}
    if message.role == Role.ASSISTANT:
        payload: dict = {"role": "assistant", "content": message.content or None}
        if message.tool_calls:
            payload["tool_calls"] = _serialize_tool_calls(message.tool_calls)
        # Echo back the provider's thinking blocks so interleaved thinking is
        # preserved across tool-call turns (required by Anthropic when a prior
        # assistant turn produced thinking + tool_use). Only for providers that
        # accept them on the way back in — others don't need/allow it.
        if include_thinking:
            blocks = message.metadata.get("thinking_blocks")
            if blocks:
                payload["thinking_blocks"] = blocks
        # Providers outside the Anthropic thinking-block shape return their chain
        # of thought as flat `reasoning_content`. Without echoing it, a reasoning
        # model re-derives its thinking from nothing every turn: measured on the
        # 2026-07-30 4-task set, 10,770 reasoning tokens were generated, logged
        # and discarded, and two tasks rewrote the same file three times over.
        # It rides in the cached prefix, so carrying it costs ~11% of the fresh
        # input rate. `reasoning_content` is a declared field on litellm's
        # request-side assistant message, so it survives the OpenAI-compatible
        # transform rather than being dropped as an unknown key.
        elif include_reasoning:
            reasoning = message.metadata.get("reasoning_content")
            if reasoning:
                payload["reasoning_content"] = reasoning
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
    reasoning = getattr(delta, "reasoning_content", None)
    if reasoning:
        deltas.append(StreamDelta(reasoning_delta=reasoning))
    tblocks = _normalize_thinking_blocks(getattr(delta, "thinking_blocks", None))
    if tblocks:
        deltas.append(StreamDelta(thinking_blocks=tblocks))
    for tc in getattr(delta, "tool_calls", None) or []:
        fn = getattr(tc, "function", None)
        deltas.append(
            StreamDelta(
                tool_call_delta={
                    # Preserve a missing index as None rather than coercing to 0 —
                    # coercion is what merged every index-less call into one slot.
                    "index": getattr(tc, "index", None),
                    "id": getattr(tc, "id", None),
                    "name": getattr(fn, "name", None) if fn is not None else None,
                    "arguments": getattr(fn, "arguments", None) if fn is not None else None,
                }
            )
        )
    return deltas


def _merge_tool_fragment(tool_frags: dict, frag: dict) -> None:
    """Fold a streamed tool-call fragment into per-call accumulators.

    ``index`` is the natural key, but not every provider sends one. Coercing a
    missing index to 0 (as this did) merged *every* parallel tool call of the turn
    into a single slot, so the model's second and third calls silently vanished
    and their arguments were concatenated onto the first.
    """
    index = frag.get("index")
    if index is not None:
        key: object = index
    elif frag.get("id"):
        # No index: the id identifies the call.
        key = f"id:{frag['id']}"
    elif tool_frags:
        # Neither index nor id: a bare argument fragment continuing the open call.
        key = next(reversed(tool_frags))
    else:
        key = 0
    slot = tool_frags.setdefault(key, {"id": None, "name": None, "arguments": ""})
    _merge_slot(slot, frag)


def _ordered_slots(tool_frags: dict) -> list[dict]:
    """Accumulated calls in provider order.

    Sorting is only meaningful when every key is an index; a mixed int/str key set
    (some providers send an index, some only an id) would raise on comparison, so
    fall back to insertion order — which is the arrival order anyway.
    """
    if tool_frags and all(isinstance(key, int) for key in tool_frags):
        return [slot for _, slot in sorted(tool_frags.items())]
    return list(tool_frags.values())


def _merge_slot(slot: dict, frag: dict) -> None:
    if frag.get("id"):
        slot["id"] = frag["id"]
    if frag.get("name"):
        slot["name"] = frag["name"]
    if frag.get("arguments"):
        slot["arguments"] += frag["arguments"]


def _provider_cost(response, raw_usage) -> float | None:
    """The cost the provider billed for this call, when it says so.

    Worth reaching for: a public pricing table can be stale or simply wrong for
    a given model, and a confidently wrong cost is worse than none. OpenRouter
    reports `usage.cost`; others expose it through litellm's hidden params.
    """
    for candidate in (
        getattr(raw_usage, "cost", None),
        (getattr(response, "_hidden_params", None) or {}).get("response_cost"),
    ):
        if isinstance(candidate, (int, float)) and candidate >= 0:
            return float(candidate)
    return None


def _extract_usage(response) -> dict[str, float]:
    usage: dict[str, float] = {}
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
    cost = _provider_cost(response, raw)
    if cost is not None:
        usage["cost_usd"] = cost
    return usage


class LitellmModel:
    def __init__(
        self,
        model_name: str,
        api_key: str | None = None,
        api_base: str | None = None,
        max_retries: int = 5,
        request_timeout: float = 600.0,
        enable_prompt_caching: bool = True,
        reasoning_effort: str | None = None,
        thinking_budget_tokens: int | None = None,
        preserve_reasoning: bool = False,
    ):
        self._model_name = model_name
        self._api_key = api_key
        self._api_base = api_base
        self._max_retries = max_retries
        self._request_timeout = request_timeout
        self._enable_prompt_caching = enable_prompt_caching
        # Extended-thinking knobs. ``reasoning_effort`` (minimal|low|medium|high)
        # is litellm's cross-provider knob; ``thinking_budget_tokens`` sets an
        # explicit Anthropic thinking budget. Either enables reasoning.
        self._reasoning_effort = reasoning_effort
        self._thinking_budget_tokens = thinking_budget_tokens
        # Echo the model's own prior reasoning back across tool-call turns.
        # Independent of whether *we* asked for reasoning: a model that thinks by
        # default (minimax-m2.5 does) produces it either way. Opt-in — see
        # AgentConfig.preserve_reasoning for the measurement behind the default.
        self._preserve_reasoning = preserve_reasoning
        self._vision_support: bool | None = None

    @classmethod
    def from_config(cls, model_name: str, config, **overrides) -> "LitellmModel":
        """Build a model, pulling reasoning knobs off an AgentConfig when present."""
        params = {
            "reasoning_effort": getattr(config, "reasoning_effort", None),
            "thinking_budget_tokens": getattr(config, "thinking_budget_tokens", None),
            "preserve_reasoning": getattr(config, "preserve_reasoning", True),
        }
        params.update(overrides)
        return cls(model_name=model_name, **params)

    @property
    def model_name(self) -> str:
        return self._model_name

    @property
    def supports_tool_calling(self) -> bool:
        return True

    def _is_anthropic(self) -> bool:
        name = self._model_name.lower()
        return name.startswith("anthropic/") or "claude" in name

    def _supports_vision(self) -> bool:
        """Whether the model accepts image content blocks (cached)."""
        if self._vision_support is None:
            try:
                self._vision_support = bool(litellm.supports_vision(model=self._model_name))
            except Exception:
                self._vision_support = False
        return self._vision_support

    def _reasoning_enabled(self) -> bool:
        return bool(self._reasoning_effort or self._thinking_budget_tokens)

    def _supports_cache_control(self) -> bool:
        return self._enable_prompt_caching and self._is_anthropic()

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
        litellm_messages = [
            _message_to_litellm(m, **self._serialization_flags()) for m in messages
        ]
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
        self._apply_usage_accounting(kwargs)
        self._apply_reasoning(kwargs)
        return kwargs

    def _apply_usage_accounting(self, kwargs: dict) -> None:
        """Ask OpenRouter to return what the call actually cost.

        Opt-in per request, and free: it adds a `cost` field to the usage block.
        Having the real figure removes any dependence on a public pricing table
        being right about this model — including the cache-read rate, which is
        where estimates go furthest wrong on a cache-heavy agentic run.
        """
        if not self._model_name.startswith("openrouter/"):
            return
        extra_body = kwargs.setdefault("extra_body", {})
        if isinstance(extra_body, dict):
            extra_body.setdefault("usage", {"include": True})

    def _serialization_flags(self) -> dict[str, bool]:
        """How this provider wants messages rendered.

        One source of truth so the request path and the token counter cannot
        disagree about what is in the prompt. Anthropic carries reasoning as
        thinking_blocks; everyone else as flat reasoning_content — exactly one of
        the two applies to a given provider.
        """
        return {
            "include_thinking": self._is_anthropic() and self._reasoning_enabled(),
            "include_reasoning": self._preserve_reasoning and not self._is_anthropic(),
            "include_images": self._supports_vision(),
        }

    def _apply_reasoning(self, kwargs: dict) -> None:
        """Attach extended-thinking params. ``drop_params`` lets litellm silently
        ignore them on models that don't support reasoning, so the same profile
        works across a reasoning and a non-reasoning model without 400s."""
        if not self._reasoning_enabled():
            return
        if self._thinking_budget_tokens:
            budget = self._thinking_budget_tokens
            kwargs["thinking"] = {"type": "enabled", "budget_tokens": budget}
            # Anthropic requires max_tokens > thinking budget; ensure headroom.
            current_max = kwargs.get("max_tokens")
            if current_max is None or current_max <= budget:
                kwargs["max_tokens"] = budget + 4096
            # Anthropic rejects temperature != 1 when thinking is on.
            kwargs.pop("temperature", None)
        elif self._reasoning_effort:
            kwargs["reasoning_effort"] = self._reasoning_effort
        kwargs["drop_params"] = True

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
        reasoning_content = getattr(message, "reasoning_content", None)
        thinking_blocks = _normalize_thinking_blocks(getattr(message, "thinking_blocks", None))

        return ModelResponse(
            content=content,
            tool_calls=tool_calls,
            raw={
                "model": self._model_name,
                "finish_reason": getattr(choice, "finish_reason", None),
            },
            usage=_extract_usage(response),
            reasoning_content=reasoning_content,
            thinking_blocks=thinking_blocks,
        )

    async def _complete_with_retries(self, kwargs: dict):
        # At least one attempt even if max_retries is 0 (otherwise the loop body
        # never runs and we would `raise None`).
        attempts = max(1, self._max_retries)
        delay = 1.0
        last_exc: Exception | None = None
        governor = get_governor()
        provider = provider_of(self._model_name)
        for attempt in range(1, attempts + 1):
            try:
                # Acquire the slot per attempt so a request sleeping on backoff
                # releases its slot instead of starving the rest of the fleet.
                async with governor.slot(provider):
                    return await litellm.acompletion(**kwargs)
            except Exception as exc:
                # Non-transient errors (400/401/404, context-window, etc.) fail fast.
                if not _is_retryable(exc):
                    raise
                last_exc = exc
                if attempt == attempts:
                    break
                retry_after = _retry_after_seconds(exc)
                base = retry_after if retry_after is not None else min(delay, _MAX_RETRY_SLEEP)
                # Full-ish jitter so parallel subagents don't retry in lockstep.
                wait = base + random.uniform(0, min(base, 1.0))
                logger.warning(
                    "Model call failed (%s), retry %d/%d in %.1fs",
                    type(exc).__name__,
                    attempt,
                    attempts - 1,
                    wait,
                )
                await asyncio.sleep(wait)
                delay = min(delay * 2, _MAX_RETRY_SLEEP)
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
        # Ask the provider to emit a final usage chunk so streamed runs still get
        # token/cost accounting (otherwise usage is lost for TUI sessions).
        kwargs["stream_options"] = {"include_usage": True}
        response_stream = await self._complete_with_retries(kwargs)
        async for chunk in response_stream:
            for delta in _stream_deltas_from_chunk(chunk):
                yield delta
            usage = _extract_usage(chunk)
            if usage:
                yield StreamDelta(usage=usage)
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
        reasoning_parts: list[str] = []
        thinking_blocks: list[dict] | None = None
        tool_frags: dict = {}
        usage: dict[str, int] = {}
        try:
            async for delta in self.stream(messages, tools, temperature, max_tokens):
                if delta.content_delta:
                    content_parts.append(delta.content_delta)
                    if on_delta is not None:
                        result = on_delta(delta.content_delta)
                        if inspect.isawaitable(result):
                            await result
                if delta.reasoning_delta:
                    reasoning_parts.append(delta.reasoning_delta)
                if delta.thinking_blocks:
                    # Streamed thinking blocks arrive assembled per chunk; keep the
                    # latest complete set (loop uses complete(), so this is best-effort).
                    thinking_blocks = delta.thinking_blocks
                if delta.tool_call_delta:
                    _merge_tool_fragment(tool_frags, delta.tool_call_delta)
                if delta.usage:
                    usage = delta.usage
        except Exception:
            # A stream that dies mid-response cannot be safely resumed; fall back to
            # a fresh non-streaming completion rather than returning a partial answer.
            logger.warning("Streaming failed mid-response; retrying without streaming", exc_info=True)
            return await self.complete(messages, tools, temperature, max_tokens)

        content = "".join(content_parts) or None
        raw_calls = [
            {
                "id": slot["id"] or str(uuid.uuid4()),
                "function": {"name": slot["name"] or "", "arguments": slot["arguments"] or "{}"},
            }
            for slot in _ordered_slots(tool_frags)
        ]
        return ModelResponse(
            content=content,
            tool_calls=_parse_tool_calls(raw_calls),
            raw={"model": self._model_name, "streamed": True},
            usage=usage,
            reasoning_content="".join(reasoning_parts) or None,
            thinking_blocks=thinking_blocks,
        )

    def count_tokens(self, messages: list[Message]) -> int:
        # Serialize exactly as the request path does. Counting a different shape
        # than we send is the undercount the fallback below warns about: once
        # reasoning is echoed back it is part of the prompt, and a counter blind
        # to it lets the window overflow instead of compacting.
        try:
            return litellm.token_counter(
                model=self._model_name,
                messages=[
                    _message_to_litellm(m, **self._serialization_flags()) for m in messages
                ],
            )
        except Exception:
            # Content alone undercounts badly: tool-call arguments are often the
            # largest part of a turn (a full-file write_file, a long bash command),
            # and this estimate gates compaction. Undercounting here means the
            # window overflows instead of compacting.
            parts: list[str] = []
            for message in messages:
                if message.content:
                    parts.append(message.content)
                for call in getattr(message, "tool_calls", None) or []:
                    parts.append(getattr(call, "name", "") or "")
                    arguments = getattr(call, "arguments", None)
                    if arguments is not None:
                        parts.append(
                            arguments if isinstance(arguments, str) else json.dumps(
                                arguments, default=str
                            )
                        )
            return sum(len(p) for p in parts) // 4
