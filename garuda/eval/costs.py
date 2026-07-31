"""Token-cost accounting and timestamp helpers shared by ATIF export and the dashboard.

Cost is resolved in four tiers, strongest first:

1. **What the provider charged.** Some providers return the actual cost of a call
   alongside its token counts. That is the invoice, not a model of it, so it wins
   outright.
2. **A local price override.** ``GARUDA_TOKEN_PRICES`` lets a caller pin rates it
   has measured for its own account.
3. **The in-repo pricing snapshot** (:mod:`garuda.eval.pricing`) — versioned,
   offline, and changed only by a reviewed diff.
4. **litellm's pricing table**, for models the snapshot does not name.

Tier 3 exists because tier 4 is not reproducible. litellm fetches its cost map
over the network at import time and it is edited continuously upstream, so the
same trajectory could price differently on two machines on the same day. That is
fine for a rough estimate and disqualifying for a benchmark number, so the
snapshot answers first and litellm only fills the gaps — with its network fetch
disabled (see :data:`_LITELLM_LOCAL_MAP_ENV`) so even the fallback depends on
nothing but the installed version.

The cache-aware part matters more than it sounds. An agentic run is mostly cache
reads — 97% of prompt tokens on a recent 50-task benchmark — and those are billed
at a fraction of the fresh-input rate. Charging every prompt token at full price,
as this module previously did, overstated spend roughly fivefold.
"""

import json
import logging
import os
from datetime import datetime
from typing import Any

from garuda.eval.pricing import snapshot_rates

logger = logging.getLogger(__name__)

# Per-model rate overrides, in USD per million tokens:
#   GARUDA_TOKEN_PRICES='{"minimax-m2.5": {"input": 0.302, "cache_read": 0.033, "output": 1.33}}'
# Keys are matched as substrings of the model name (longest match wins), so
# "minimax-m2.5" covers "openrouter/minimax/minimax-m2.5". Absent keys fall back
# to the pricing table.
_PRICE_ENV_VAR = "GARUDA_TOKEN_PRICES"

_PRICE_FIELDS = ("input", "cache_read", "cache_write", "output")

# Set before litellm is first imported so it loads the table bundled with the
# installed wheel instead of fetching one from GitHub. `setdefault`: a host that
# has deliberately chosen the network map keeps it, and if litellm is already
# imported this is a harmless no-op (the map is read at import time).
_LITELLM_LOCAL_MAP_ENV = "LITELLM_LOCAL_MODEL_COST_MAP"


def _import_litellm():
    os.environ.setdefault(_LITELLM_LOCAL_MAP_ENV, "True")
    import litellm

    return litellm


def _load_overrides() -> dict[str, dict[str, float]]:
    raw = os.environ.get(_PRICE_ENV_VAR)
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        logger.warning("%s is not valid JSON; ignoring price overrides", _PRICE_ENV_VAR)
        return {}
    if not isinstance(parsed, dict):
        logger.warning("%s must be a JSON object; ignoring price overrides", _PRICE_ENV_VAR)
        return {}
    cleaned: dict[str, dict[str, float]] = {}
    for model, rates in parsed.items():
        if not isinstance(rates, dict):
            continue
        entry = {}
        for field in _PRICE_FIELDS:
            value = rates.get(field)
            if isinstance(value, (int, float)):
                entry[field] = float(value) / 1e6  # given per million
        if entry:
            cleaned[str(model)] = entry
    return cleaned


def _override_for(model_name: str) -> dict[str, float] | None:
    overrides = _load_overrides()
    if not overrides:
        return None
    matches = [key for key in overrides if key and key in model_name]
    if not matches:
        return None
    return overrides[max(matches, key=len)]


def _apply_rates(
    rates: dict[str, float], fresh: int, cache_read: int, cache_write: int, completion: int
) -> float:
    """Price a token split against one rate table.

    A rate table need not be complete: a provider that does not bill cache reads
    separately simply has none, and those tokens are charged at the input rate
    rather than silently priced at zero.
    """
    input_rate = rates.get("input", 0.0)
    return (
        fresh * input_rate
        + cache_read * rates.get("cache_read", input_rate)
        + cache_write * rates.get("cache_write", input_rate)
        + completion * rates.get("output", 0.0)
    )


def provider_cost(usage: dict[str, Any] | None) -> float | None:
    """The cost the provider itself reported for a call, if it reported one."""
    if not usage:
        return None
    value = usage.get("cost_usd")
    if isinstance(value, (int, float)) and value >= 0:
        return float(value)
    return None


def _split_tokens(usage: dict[str, Any]) -> tuple[int, int, int, int]:
    """(fresh_input, cache_read, cache_write, output) from a usage dict.

    ``prompt_tokens`` is inclusive of cached reads everywhere we consume it, so
    the fresh count is the remainder — floored at zero in case a provider reports
    the two inconsistently.
    """
    prompt = int(usage.get("prompt_tokens", 0) or 0)
    completion = int(usage.get("completion_tokens", 0) or 0)
    cache_read = int(usage.get("cache_read_tokens", 0) or 0)
    cache_write = int(usage.get("cache_creation_tokens", 0) or 0)
    fresh = max(prompt - cache_read, 0)
    return fresh, cache_read, cache_write, completion


def estimate_cost(model_name: str | None, usage: dict[str, Any] | None) -> float | None:
    """USD cost for one model call.

    Returns None when no tier can price it, so callers omit the figure rather
    than publish a wrong one.
    """
    if not usage:
        return None

    reported = provider_cost(usage)
    if reported is not None:
        return round(reported, 8)

    if not model_name:
        return None

    fresh, cache_read, cache_write, completion = _split_tokens(usage)
    if not any((fresh, cache_read, cache_write, completion)):
        return None

    rates = _override_for(model_name) or snapshot_rates(model_name)
    if rates is not None:
        return round(_apply_rates(rates, fresh, cache_read, cache_write, completion), 8)

    try:
        litellm = _import_litellm()

        # litellm treats prompt_tokens as inclusive and discounts the cached
        # portion internally, so it is given the original prompt count.
        prompt_cost, completion_cost = litellm.cost_per_token(
            model=model_name,
            prompt_tokens=fresh + cache_read,
            completion_tokens=completion,
            cache_read_input_tokens=cache_read,
            cache_creation_input_tokens=cache_write,
        )
        return round(prompt_cost + completion_cost, 8)
    except TypeError:
        # Older litellm without the cache parameters: price the fresh tokens
        # only. Undercounting the discounted portion beats charging it at the
        # full input rate, which is the error this module used to make.
        try:
            litellm = _import_litellm()

            prompt_cost, completion_cost = litellm.cost_per_token(
                model=model_name, prompt_tokens=fresh, completion_tokens=completion
            )
            return round(prompt_cost + completion_cost, 8)
        except Exception:
            return None
    except Exception:
        return None


def parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None


def duration_ms(start: str | None, end: str | None) -> int | None:
    """Wall-clock milliseconds between two ISO timestamps, or None."""
    start_dt = parse_timestamp(start)
    end_dt = parse_timestamp(end)
    if start_dt is None or end_dt is None:
        return None
    delta = (end_dt - start_dt).total_seconds() * 1000
    return int(delta) if delta >= 0 else None


def merge_usage(target: dict[str, int], usage: dict[str, Any] | None) -> None:
    """Accumulate token counts from one usage dict into a running total."""
    if not usage:
        return
    for key in ("prompt_tokens", "completion_tokens", "total_tokens",
                "cache_read_tokens", "cache_creation_tokens"):
        value = usage.get(key)
        if value:
            target[key] = target.get(key, 0) + int(value)
