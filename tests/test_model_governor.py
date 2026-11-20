"""C3: model-concurrency governor — per-provider in-flight cap."""

import asyncio

import pytest

from garuda.model.governor import ModelGovernor, provider_of


def test_provider_of():
    # A multi-segment name still buckets on the first component only: everything
    # behind one gateway shares a rate limit, so it has to share a bucket.
    assert provider_of("openrouter/deepseek/deepseek-v4-flash-0731") == "openrouter"
    assert provider_of("openai/gpt-4o-mini") == "openai"
    assert provider_of("bare-model") == "bare-model"
    assert provider_of("") == ""


async def _run_workers(gov: ModelGovernor, provider: str, n: int, hold: float = 0.03):
    active = 0
    peak = 0

    async def worker():
        nonlocal active, peak
        async with gov.slot(provider):
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(hold)
            active -= 1

    await asyncio.gather(*[worker() for _ in range(n)])
    return peak


async def test_unlimited_by_default_allows_all_concurrent():
    gov = ModelGovernor(limit=0)
    peak = await _run_workers(gov, "p", 6)
    assert peak == 6  # no cap


async def test_limit_caps_peak_concurrency():
    gov = ModelGovernor(limit=2)
    peak = await _run_workers(gov, "p", 6)
    assert peak == 2


async def test_providers_are_isolated():
    gov = ModelGovernor(limit=1)
    active: dict[str, int] = {}
    peak_total = 0

    async def worker(provider: str):
        nonlocal peak_total
        async with gov.slot(provider):
            active[provider] = active.get(provider, 0) + 1
            peak_total = max(peak_total, sum(active.values()))
            await asyncio.sleep(0.05)
            active[provider] -= 1

    # limit=1 serializes within each provider, but "a" and "b" run concurrently.
    await asyncio.gather(worker("a"), worker("a"), worker("b"), worker("b"))
    assert peak_total == 2


async def test_set_limit_takes_effect():
    gov = ModelGovernor(limit=0)
    assert await _run_workers(gov, "p", 4) == 4
    gov.set_limit(1)
    assert await _run_workers(gov, "p", 4) == 1


async def test_complete_with_retries_respects_governor(monkeypatch):
    import litellm

    from garuda.model import governor as gov_mod
    from garuda.model.litellm_model import LitellmModel

    active = 0
    peak = 0

    async def fake_acompletion(**kwargs):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.03)
        active -= 1
        return "ok"  # _complete_with_retries returns the raw response unmodified

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
    # Reset the process-wide singleton so this test controls the limit.
    monkeypatch.setattr(gov_mod, "_governor", None)
    gov_mod.set_max_concurrency(2)
    try:
        model = LitellmModel(model_name="openrouter/x/y", max_retries=1)
        results = await asyncio.gather(
            *[model._complete_with_retries({}) for _ in range(6)]
        )
        assert results == ["ok"] * 6
        assert peak == 2
    finally:
        gov_mod.set_max_concurrency(0)


@pytest.mark.parametrize("cancel", [False, True], ids=["complete", "cancel"])
async def test_stream_holds_provider_slot_until_iterator_cleanup(monkeypatch, cancel):
    import litellm

    from garuda.model import governor
    from garuda.model.litellm_model import LitellmModel

    reading = asyncio.Event()
    finish_reading = asyncio.Event()
    cleaning = asyncio.Event()
    finish_cleanup = asyncio.Event()
    second_attempted = asyncio.Event()
    second_dispatched = asyncio.Event()

    class Stream:
        def __aiter__(self):
            return self

        async def __anext__(self):
            reading.set()
            await finish_reading.wait()
            raise StopAsyncIteration

        async def aclose(self):
            cleaning.set()
            await finish_cleanup.wait()

    async def empty_stream():
        for item in ():
            yield item

    async def provider(**request):
        if request["model"].endswith("/first"):
            return Stream()
        second_dispatched.set()
        return empty_stream()

    monkeypatch.setattr(litellm, "acompletion", provider)
    monkeypatch.setattr(governor, "_governor", ModelGovernor(limit=1))

    async def consume(name):
        if name == "second":
            second_attempted.set()
        async for _ in LitellmModel(model_name=f"openai/{name}").stream([]):
            pass

    first = asyncio.create_task(consume("first"))
    second = None
    try:
        await asyncio.wait_for(reading.wait(), 3)
        second = asyncio.create_task(consume("second"))
        await asyncio.wait_for(second_attempted.wait(), 3)
        assert not second_dispatched.is_set()
        if cancel:
            first.cancel()
        else:
            finish_reading.set()
        await asyncio.wait_for(cleaning.wait(), 3)
        if cancel:
            first.cancel()
            await asyncio.sleep(0)
        assert not second_dispatched.is_set() and not first.done()
        finish_cleanup.set()
        await asyncio.wait_for(second, 3)
        assert second_dispatched.is_set()
        if cancel:
            with pytest.raises(asyncio.CancelledError):
                await first
        else:
            await first
    finally:
        finish_reading.set()
        finish_cleanup.set()
        await asyncio.gather(first, *([second] if second is not None else []), return_exceptions=True)


async def test_failed_stream_attempt_releases_slot_during_backoff(monkeypatch):
    import litellm

    from garuda.model import governor, litellm_model
    from garuda.model.litellm_model import LitellmModel

    class Retryable(Exception):
        status_code = 503

    first_failed = asyncio.Event()
    second_dispatched = asyncio.Event()
    release_second = asyncio.Event()
    first_calls = 0

    class Stream:
        def __aiter__(self):
            return self

        async def __anext__(self):
            await release_second.wait()
            raise StopAsyncIteration

    async def empty_stream():
        for item in ():
            yield item

    async def provider(**request):
        nonlocal first_calls
        if request["model"].endswith("/first"):
            first_calls += 1
            if first_calls == 1:
                first_failed.set()
                raise Retryable("unavailable")
            return empty_stream()
        second_dispatched.set()
        return Stream()

    monkeypatch.setattr(litellm, "acompletion", provider)
    monkeypatch.setattr(litellm_model, "_MAX_RETRY_SLEEP", 0.5)
    monkeypatch.setattr(governor, "_governor", ModelGovernor(limit=1))

    async def consume(name):
        async for _ in LitellmModel(model_name=f"openai/{name}", max_retries=2).stream([]):
            pass

    first = asyncio.create_task(consume("first"))
    second = None
    try:
        await asyncio.wait_for(first_failed.wait(), 3)
        second = asyncio.create_task(consume("second"))
        # The first stream is sleeping on backoff, so the second gets the slot.
        await asyncio.wait_for(second_dispatched.wait(), 3)
        assert first_calls == 1
        release_second.set()
        await asyncio.wait_for(asyncio.gather(first, second), 5)
        assert first_calls == 2
    finally:
        release_second.set()
        await asyncio.gather(first, *([second] if second is not None else []), return_exceptions=True)


async def test_cancelled_stream_releases_slot_when_close_hangs(monkeypatch, caplog):
    import litellm

    from garuda.model import governor, litellm_model
    from garuda.model.litellm_model import LitellmModel

    reading = asyncio.Event()
    cleaning = asyncio.Event()
    finish_cleanup = asyncio.Event()
    second_dispatched = asyncio.Event()

    class Stream:
        def __aiter__(self):
            return self

        async def __anext__(self):
            reading.set()
            await asyncio.Event().wait()

        async def aclose(self):
            cleaning.set()
            await finish_cleanup.wait()
            raise RuntimeError("late close failure")

    async def empty_stream():
        for item in ():
            yield item

    async def provider(**request):
        if request["model"].endswith("/first"):
            return Stream()
        second_dispatched.set()
        return empty_stream()

    monkeypatch.setattr(litellm, "acompletion", provider)
    monkeypatch.setattr(litellm_model, "_STREAM_CLOSE_GRACE", 0.2)
    monkeypatch.setattr(governor, "_governor", ModelGovernor(limit=1))

    async def consume(name):
        async for _ in LitellmModel(model_name=f"openai/{name}").stream([]):
            pass

    first = asyncio.create_task(consume("first"))
    second = None
    try:
        await asyncio.wait_for(reading.wait(), 3)
        second = asyncio.create_task(consume("second"))
        await asyncio.sleep(0.05)
        assert not second_dispatched.is_set()
        first.cancel()
        await asyncio.wait_for(cleaning.wait(), 3)
        # Inside the grace period the slot stays held.
        await asyncio.sleep(0.05)
        assert not second_dispatched.is_set() and not first.done()
        # A close that hangs past the grace period gives up the slot.
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(asyncio.shield(first), 3)
        await asyncio.wait_for(second, 3)
        assert second_dispatched.is_set()
        background = set(litellm_model._background_closes)
        assert len(background) == 1
        with caplog.at_level("WARNING", logger=litellm_model.__name__):
            finish_cleanup.set()
            await asyncio.gather(*background, return_exceptions=True)
            await asyncio.sleep(0)
        assert not litellm_model._background_closes
        assert "late close failure" in caplog.text
    finally:
        finish_cleanup.set()
        await asyncio.gather(first, *([second] if second is not None else []), return_exceptions=True)
