"""Model-layer resilience: retry classification, Retry-After, max_retries=0,
non-retryable fast-fail, and mid-stream fallback."""

import asyncio

import litellm
import pytest

import garuda.model.litellm_model as lm
from garuda.model.litellm_model import (
    LitellmModel,
    _is_retryable,
    _retry_after_seconds,
)
from garuda.model.protocol import ModelResponse
from garuda.types import Message, Role


class _StatusExc(Exception):
    def __init__(self, status_code):
        super().__init__(f"status {status_code}")
        self.status_code = status_code


class _RetryAfterExc(Exception):
    def __init__(self, retry_after=None, header=None):
        super().__init__("rate limited")
        if retry_after is not None:
            self.retry_after = retry_after
        if header is not None:
            self.response = type("R", (), {"headers": {"retry-after": header}})()


def test_is_retryable_by_type():
    assert _is_retryable(litellm.RateLimitError("x", llm_provider="p", model="m"))


def test_is_retryable_by_status_code():
    assert _is_retryable(_StatusExc(529))   # overloaded
    assert _is_retryable(_StatusExc(503))
    assert not _is_retryable(_StatusExc(400))  # bad request
    assert not _is_retryable(_StatusExc(401))
    assert not _is_retryable(ValueError("nope"))


def test_retry_after_from_attr_and_header():
    assert _retry_after_seconds(_RetryAfterExc(retry_after=5)) == 5.0
    assert _retry_after_seconds(_RetryAfterExc(header="12")) == 12.0
    assert _retry_after_seconds(_RetryAfterExc(retry_after=99999)) == lm._MAX_RETRY_SLEEP  # capped
    assert _retry_after_seconds(ValueError("none")) is None


async def test_max_retries_zero_raises_real_exception(monkeypatch):
    async def always_rate_limited(**kwargs):
        raise litellm.RateLimitError("rl", llm_provider="p", model="m")

    monkeypatch.setattr(lm.litellm, "acompletion", always_rate_limited)
    model = LitellmModel("openai/gpt-4o-mini", max_retries=0)
    # Must raise the actual error, NOT TypeError from `raise None`.
    with pytest.raises(litellm.RateLimitError):
        await model.complete([Message(role=Role.USER, content="hi")])


async def test_non_retryable_fails_fast(monkeypatch):
    calls = {"n": 0}

    async def bad_request(**kwargs):
        calls["n"] += 1
        raise _StatusExc(400)

    monkeypatch.setattr(lm.litellm, "acompletion", bad_request)
    model = LitellmModel("openai/gpt-4o-mini", max_retries=5)
    with pytest.raises(_StatusExc):
        await model.complete([Message(role=Role.USER, content="hi")])
    assert calls["n"] == 1  # no wasted retries on a 400


async def test_transient_status_is_retried(monkeypatch):
    calls = {"n": 0}

    async def flaky(**kwargs):
        calls["n"] += 1
        if calls["n"] < 3:
            raise _StatusExc(529)
        return _fake_response("done")

    async def no_sleep(_):
        return None

    monkeypatch.setattr(lm.litellm, "acompletion", flaky)
    monkeypatch.setattr(lm.asyncio, "sleep", no_sleep)
    model = LitellmModel("openai/gpt-4o-mini", max_retries=5)
    resp = await model.complete([Message(role=Role.USER, content="hi")])
    assert resp.content == "done"
    assert calls["n"] == 3


async def test_complete_streaming_falls_back_on_midstream_error(monkeypatch):
    model = LitellmModel("openai/gpt-4o-mini")

    async def dying_stream(*args, **kwargs):
        from garuda.model.protocol import StreamDelta

        yield StreamDelta(content_delta="par")
        raise litellm.APIConnectionError("dropped", llm_provider="p", model="m")

    async def fake_complete(messages, tools=None, temperature=None, max_tokens=None):
        return ModelResponse(content="full answer", tool_calls=[])

    monkeypatch.setattr(model, "stream", dying_stream)
    monkeypatch.setattr(model, "complete", fake_complete)
    resp = await model.complete_streaming([Message(role=Role.USER, content="hi")])
    assert resp.content == "full answer"  # fell back to non-streaming complete


def _fake_response(text):
    from types import SimpleNamespace

    msg = SimpleNamespace(content=text, tool_calls=[], reasoning_content=None, thinking_blocks=None)
    choice = SimpleNamespace(message=msg, finish_reason="stop")
    return SimpleNamespace(choices=[choice], usage=None)
