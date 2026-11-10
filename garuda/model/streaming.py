"""One streamed model call, assembled into the response ``complete()`` would return.

The loop and ``LitellmModel.complete_streaming`` share this accumulator, so a
streamed turn reaches tool dispatch, accounting and the transcript through the
same ``ModelResponse`` shape as an ordinary one. Only text and reasoning are
forwarded live; tool calls exist only once the stream has finished, and an
interrupted stream never yields a response built from its fragments.

A stream that fails before its terminal chunk falls back once to ``complete()``.
The consumer is told that what it has already been shown is discarded; the
fallback's answer is not re-sent as deltas, so a reader renders it from the
``model_response`` event, whose ``live_coverage`` says which of the two it saw.
Cancellation is never caught: it propagates with nothing assembled or replayed.
"""

import inspect
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from garuda.model.litellm_model import _merge_tool_fragment, _ordered_slots, _parse_tool_calls
from garuda.model.protocol import ContextOverflowError, ModelResponse, StreamDelta, supports_streaming
from garuda.model.usage import attach_usage, attempts_for, sum_usage, usage_attempt
from garuda.types import Message

logger = logging.getLogger(__name__)

STREAMED = "streamed"
FALLBACK = "fallback"
NOT_STREAMED = "not_streamed"

# Bounded so a provider's error body cannot grow an event without limit.
_ERROR_CHARS = 300


@dataclass(frozen=True)
class LiveDelta:
    """What a delta consumer receives during one model call.

    ``kind`` is ``text`` or ``reasoning`` (``text`` holds the new fragment), or
    ``discard``: everything shown for this call so far was not the answer.
    """

    kind: str
    text: str = ""


DeltaConsumer = Callable[[LiveDelta], Awaitable[None] | None]


@dataclass
class LiveCall:
    response: ModelResponse
    # streamed | fallback | not_streamed
    coverage: str
    # "<type>: <message>" of the stream failure a fallback recovered from.
    stream_error: str | None = None
    # Type name of the error that detached the consumer, if it raised.
    consumer_error: str | None = None

    def metadata(self) -> dict:
        return live_metadata(self.coverage, self.stream_error, self.consumer_error)


def live_metadata(coverage: str, stream_error: str | None = None,
                  consumer_error: str | None = None) -> dict:
    meta: dict = {"live_coverage": coverage}
    if stream_error is not None:
        meta["stream_error"] = stream_error
    if consumer_error is not None:
        meta["live_consumer_error"] = consumer_error
    return meta


class IncompleteStreamError(Exception):
    """The stream ended without its terminal chunk, so its output is not a whole answer."""


class _Forwarder:
    """Delivers deltas to the consumer; a consumer that raises is detached, not fatal."""

    def __init__(self, consumer: DeltaConsumer | None):
        self._consumer = consumer
        self.shown = False
        self.error: str | None = None

    async def send(self, delta: LiveDelta) -> None:
        if self._consumer is None:
            return
        try:
            result = self._consumer(delta)
            if inspect.isawaitable(result):
                await result
        except Exception as exc:
            # A disconnected viewer must not change the model call or its accounting.
            logger.warning("Delta consumer failed; detaching it for this call", exc_info=True)
            self._consumer = None
            self.error = type(exc).__name__
            return
        if delta.kind != "discard":
            self.shown = True


class _Accumulator:
    def __init__(self):
        self.content: list[str] = []
        self.reasoning: list[str] = []
        self.thinking_blocks: list[dict] | None = None
        self.tool_frags: dict = {}
        self.usage: dict = {}
        self.attempts: list[dict] = []
        self.finish_reason: str | None = None
        self.status: str | None = None
        self.done = False

    def add(self, delta: StreamDelta) -> None:
        if delta.content_delta:
            self.content.append(delta.content_delta)
        if delta.reasoning_delta:
            self.reasoning.append(delta.reasoning_delta)
        if delta.thinking_blocks:
            # Streamed thinking blocks arrive assembled; keep the latest complete set.
            self.thinking_blocks = delta.thinking_blocks
        if delta.tool_call_delta:
            _merge_tool_fragment(self.tool_frags, delta.tool_call_delta)
        # Usage deltas are cumulative snapshots of the logical call, not increments.
        if delta.usage:
            self.usage = delta.usage
        if delta.attempt_usage is not None:
            self.attempts = delta.attempt_usage
        if delta.finish_reason is not None:
            self.finish_reason = delta.finish_reason
        if delta.status is not None:
            self.status = delta.status
        if delta.done:
            self.done = True

    def response(self, model_name: str) -> ModelResponse:
        raw_calls = [
            {
                "id": slot["id"] or str(uuid.uuid4()),
                "function": {"name": slot["name"] or "", "arguments": slot["arguments"] or "{}"},
            }
            for slot in _ordered_slots(self.tool_frags)
        ]
        return ModelResponse(
            content="".join(self.content) or None,
            tool_calls=_parse_tool_calls(raw_calls),
            raw={
                "model": model_name,
                "streamed": True,
                "finish_reason": self.finish_reason,
                "status": self.status,
            },
            usage=self.usage,
            reasoning_content="".join(self.reasoning) or None,
            thinking_blocks=self.thinking_blocks,
            attempt_usage=self.attempts,
        )

    def failed_attempts(self, exc: BaseException) -> list[dict]:
        failed = getattr(exc, "attempt_usage", None) or self.attempts or [
            usage_attempt(self.usage, "failed")
        ]
        failed = [dict(attempt) for attempt in failed]
        failed[-1]["outcome"] = "failed"
        return failed


def _describe(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"[:_ERROR_CHARS]


async def stream_complete(
    model,
    messages: list[Message],
    tools: list[dict] | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    *,
    consumer: DeltaConsumer | None = None,
) -> LiveCall:
    """Make one logical model call, forwarding text/reasoning deltas to ``consumer``.

    A model without ``stream`` is called through ``complete`` and reported as
    ``not_streamed``. Raised errors carry ``live`` metadata alongside the usage
    attributes ``garuda.model.usage`` attaches.
    """
    # Keyword arguments, as the loop has always called ``complete``; temperature
    # only when set, so a Model without that parameter is called exactly as before.
    kwargs: dict = {"tools": tools, "max_tokens": max_tokens}
    if temperature is not None:
        kwargs["temperature"] = temperature
    forward = _Forwarder(consumer)
    if not supports_streaming(model):
        return LiveCall(await model.complete(messages, **kwargs), NOT_STREAMED)

    acc = _Accumulator()
    deltas = model.stream(messages, **kwargs)
    try:
        try:
            async for delta in deltas:
                acc.add(delta)
                if delta.reasoning_delta:
                    await forward.send(LiveDelta("reasoning", delta.reasoning_delta))
                if delta.content_delta:
                    await forward.send(LiveDelta("text", delta.content_delta))
        finally:
            close = getattr(deltas, "aclose", None)
            if close is not None:
                await close()
        if not acc.done:
            raise IncompleteStreamError("stream ended without a terminal chunk")
    except ContextOverflowError as exc:
        # Not a transport failure: the loop shrinks the prompt and asks again,
        # and repeating the oversized request without streaming would not fit either.
        if forward.shown:
            await forward.send(LiveDelta("discard"))
        exc.live = live_metadata(NOT_STREAMED, consumer_error=forward.error)
        raise
    except Exception as exc:
        # A stream that dies mid-response cannot be resumed and its fragments are
        # not an answer; one fresh non-streaming call replaces it.
        logger.warning("Streaming failed mid-response; retrying without streaming", exc_info=True)
        stream_error = _describe(exc)
        failed = acc.failed_attempts(exc)
        if forward.shown:
            await forward.send(LiveDelta("discard"))
        try:
            response = await model.complete(messages, **kwargs)
        except Exception as fallback_exc:
            attach_usage(fallback_exc, failed + attempts_for(fallback_exc, "failed"))
            fallback_exc.live = live_metadata(FALLBACK, stream_error, forward.error)
            raise
        response.attempt_usage = failed + attempts_for(response, "success")
        response.usage = sum_usage(response.attempt_usage)
        return LiveCall(response, FALLBACK, stream_error, forward.error)

    return LiveCall(acc.response(model.model_name), STREAMED, consumer_error=forward.error)
