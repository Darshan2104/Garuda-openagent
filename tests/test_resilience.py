"""Model-layer resilience: retry classification, Retry-After, max_retries=0,
non-retryable fast-fail, and mid-stream fallback."""


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

        yield StreamDelta(
            content_delta="par", finish_reason="length", status="incomplete"
        )
        raise litellm.APIConnectionError("dropped", llm_provider="p", model="m")

    async def fake_complete(messages, tools=None, temperature=None, max_tokens=None):
        return ModelResponse(
            content="full answer",
            tool_calls=[],
            raw={"finish_reason": "stop", "status": "completed"},
        )

    monkeypatch.setattr(model, "stream", dying_stream)
    monkeypatch.setattr(model, "complete", fake_complete)
    resp = await model.complete_streaming([Message(role=Role.USER, content="hi")])
    assert resp.content == "full answer"  # fell back to non-streaming complete
    assert resp.raw == {"finish_reason": "stop", "status": "completed"}


def _fake_response(text):
    from types import SimpleNamespace

    msg = SimpleNamespace(content=text, tool_calls=[], reasoning_content=None, thinking_blocks=None)
    choice = SimpleNamespace(message=msg, finish_reason="stop")
    return SimpleNamespace(choices=[choice], usage=None)


def _reported_error(status, usage=None):
    import httpx

    exc = _StatusExc(status)
    exc.response = httpx.Response(status, json={"usage": usage} if usage is not None else {})
    return exc


def _priced_response(prompt=20, completion=4, cost=0.3):
    return litellm.ModelResponse(
        choices=[{"message": {"role": "assistant", "content": "done"}, "finish_reason": "stop"}],
        usage={"prompt_tokens": prompt, "completion_tokens": completion,
               "total_tokens": prompt + completion, "cost": cost},
    )


@pytest.fixture
def no_backoff(monkeypatch):
    async def no_sleep(_):
        pass

    monkeypatch.setattr(lm.asyncio, "sleep", no_sleep)


@pytest.mark.parametrize("reported", [True, False], ids=["reported", "unknown"])
async def test_retry_usage_belongs_to_the_logical_response(monkeypatch, no_backoff, reported):
    failed_usage = {"prompt_tokens": 10, "completion_tokens": 2, "cost": 0.1} if reported else None
    replies = iter([_reported_error(529, failed_usage), _priced_response()])

    async def complete(**kwargs):
        reply = next(replies)
        if isinstance(reply, Exception):
            raise reply
        return reply

    monkeypatch.setattr(lm.litellm, "acompletion", complete)
    response = await LitellmModel("openai/gpt-4o-mini", max_retries=2).complete([])
    assert response.usage == {
        "prompt_tokens": 30 if reported else 20,
        "completion_tokens": 6 if reported else 4,
        "total_tokens": 36 if reported else 24,
        "cost_usd": pytest.approx(0.4 if reported else 0.3),
    }
    assert [a["outcome"] for a in response.attempt_usage] == ["failed", "success"]
    assert response.attempt_usage[0]["usage"] == (
        {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12, "cost_usd": 0.1}
        if reported else {}
    )


@pytest.mark.parametrize("terminal", ["exhausted", "nonretryable", "overflow"])
async def test_terminal_error_carries_all_attempt_usage(monkeypatch, no_backoff, terminal):
    from garuda.model.protocol import ContextOverflowError

    final = (_reported_error(529 if terminal == "exhausted" else 400,
                             {"completion_tokens": 3}) if terminal != "overflow" else
             litellm.ContextWindowExceededError("large", model="m", llm_provider="p"))
    replies = iter([_reported_error(529, {"prompt_tokens": 10, "cost": 0.1}), final])

    async def complete(**kwargs):
        raise next(replies)

    monkeypatch.setattr(lm.litellm, "acompletion", complete)
    expected_type = ContextOverflowError if terminal == "overflow" else _StatusExc
    with pytest.raises(expected_type) as raised:
        await LitellmModel("openai/gpt-4o-mini", max_retries=2).complete([])
    assert raised.value.usage == {
        "prompt_tokens": 10, "cost_usd": 0.1,
        **({"completion_tokens": 3} if terminal != "overflow" else {}),
    }
    assert len(raised.value.attempt_usage) == 2
    assert all(a["outcome"] == "failed" for a in raised.value.attempt_usage)
    if terminal != "overflow":
        assert raised.value is final


@pytest.mark.parametrize("fallback", ["success", "failure", "direct"])
async def test_stream_snapshots_and_failed_establishment_count_once(monkeypatch, no_backoff, fallback):
    calls = []

    async def chunks():
        for completion, cost in [(1, 0.1), (3, 0.2), (3, 0.2)]:
            yield litellm.ModelResponse(
                stream=True, choices=[],
                usage={"prompt_tokens": 10, "completion_tokens": completion,
                       "total_tokens": 10 + completion, "cost": cost},
            )
        raise _reported_error(400, {"prompt_tokens": 10, "completion_tokens": 3,
                                    "total_tokens": 13, "cost": 0.2})

    async def complete(**kwargs):
        calls.append(bool(kwargs.get("stream")))
        if len(calls) == 1:
            raise _reported_error(529, {"prompt_tokens": 5, "completion_tokens": 1, "cost": 0.05})
        if kwargs.get("stream"):
            return chunks()
        if fallback == "failure":
            raise _reported_error(400)  # fallback consumption is unknown
        return _priced_response()

    monkeypatch.setattr(lm.litellm, "acompletion", complete)
    model = LitellmModel("openai/gpt-4o-mini", max_retries=2)
    if fallback == "success":
        value = await model.complete_streaming([])
        assert value.content == "done"
        assert value.usage == {"prompt_tokens": 35, "completion_tokens": 8, "total_tokens": 43,
                               "cost_usd": pytest.approx(0.55)}
        assert [a["outcome"] for a in value.attempt_usage] == ["failed", "failed", "success"]
    else:
        with pytest.raises(_StatusExc) as raised:
            if fallback == "direct":
                _ = [delta async for delta in model.stream([])]
            else:
                await model.complete_streaming([])
        value = raised.value
        assert value.usage == {"prompt_tokens": 15, "completion_tokens": 4, "total_tokens": 19,
                               "cost_usd": pytest.approx(0.25)}
        assert all(a["outcome"] == "failed" for a in value.attempt_usage)
        assert len(value.attempt_usage) == (2 if fallback == "direct" else 3)
    assert calls == ([True, True] if fallback == "direct" else [True, True, False])


@pytest.mark.parametrize("raw, expected", [
    ({"usage": {"completion_tokens": 2}}, {"completion_tokens": 2}),
    ({"usage": litellm.Usage(completion_tokens=2)}, {"completion_tokens": 2}),
    ({"usage": {"prompt_tokens": None}}, {}),
    ({"usage": {"prompt_tokens_details": {"cached_tokens": 0}, "cache_creation_input_tokens": 0}},
     {"cache_read_tokens": 0, "cache_creation_tokens": 0}),
    ({"usage": {"prompt_tokens": 0, "completion_tokens": 0}},
     {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}),
    ({"_hidden_params": {"response_cost": 0.25}}, {"cost_usd": 0.25}),
    ({"usage": {"cost": True, "prompt_tokens": -1, "completion_tokens": True}}, {}),
    ({"usage": {"cost": float("inf")}}, {}),
])
def test_usage_extraction_keeps_missing_fields_unknown(raw, expected):
    assert lm._extract_usage(raw) == expected


async def test_response_parse_failure_retains_provider_usage(monkeypatch):
    from types import SimpleNamespace

    async def complete(**kwargs):
        return SimpleNamespace(choices=[], usage=litellm.Usage(
            prompt_tokens=10, completion_tokens=2, total_tokens=12, cost=0.1))

    monkeypatch.setattr(lm.litellm, "acompletion", complete)
    with pytest.raises(IndexError) as raised:
        await LitellmModel("openai/gpt-4o-mini").complete([])
    assert raised.value.usage == {
        "prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12, "cost_usd": 0.1,
    }
    assert raised.value.attempt_usage == [{"usage": raised.value.usage, "outcome": "failed"}]


async def test_reused_transport_error_does_not_reconsume_its_accounting(monkeypatch, no_backoff):
    error = _reported_error(529, {"prompt_tokens": 10, "cost": 0.1})

    async def complete(**kwargs):
        raise error

    monkeypatch.setattr(lm.litellm, "acompletion", complete)
    with pytest.raises(_StatusExc) as raised:
        await LitellmModel("openai/gpt-4o-mini", max_retries=3).complete([])
    assert raised.value.usage == {"prompt_tokens": 30, "cost_usd": pytest.approx(0.3)}
    assert len(raised.value.attempt_usage) == 3
