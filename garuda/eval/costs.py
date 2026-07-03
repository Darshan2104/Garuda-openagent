"""Token-cost estimation and timestamp helpers shared by ATIF export and the dashboard."""

from datetime import datetime
from typing import Any


def estimate_cost(model_name: str | None, usage: dict[str, Any] | None) -> float | None:
    """Estimate USD cost for a model call from its token usage.

    Uses litellm's per-model pricing table when available. Returns None when the
    model/pricing is unknown or litellm is unavailable, so callers can omit cost
    rather than report a wrong number.
    """
    if not model_name or not usage:
        return None
    prompt_tokens = int(usage.get("prompt_tokens", 0) or 0)
    completion_tokens = int(usage.get("completion_tokens", 0) or 0)
    if prompt_tokens == 0 and completion_tokens == 0:
        return None
    try:
        import litellm

        prompt_cost, completion_cost = litellm.cost_per_token(
            model=model_name,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        )
        return round(prompt_cost + completion_cost, 6)
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
