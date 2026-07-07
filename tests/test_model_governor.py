"""C3: model-concurrency governor — per-provider in-flight cap."""

import asyncio

import pytest

from garuda.model.governor import ModelGovernor, provider_of


def test_provider_of():
    assert provider_of("fireworks_ai/accounts/x/models/y") == "fireworks_ai"
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
        model = LitellmModel(model_name="fireworks_ai/x", max_retries=1)
        results = await asyncio.gather(
            *[model._complete_with_retries({}) for _ in range(6)]
        )
        assert results == ["ok"] * 6
        assert peak == 2
    finally:
        gov_mod.set_max_concurrency(0)
