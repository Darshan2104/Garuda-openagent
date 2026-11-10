"""Reported usage subtotals and coverage for one logical inference call."""

from typing import Any

USAGE_FIELDS = ("prompt_tokens", "completion_tokens", "total_tokens", "cost_usd")


def usage_attempt(usage: dict, outcome: str) -> dict:
    return {"usage": dict(usage), "outcome": outcome}


def attempts_for(value: Any, outcome: str) -> list[dict]:
    attempts = getattr(value, "attempt_usage", None)
    if attempts:
        return attempts
    usage = getattr(value, "usage", None)
    return [usage_attempt(usage if isinstance(usage, dict) else {}, outcome)]


def sum_usage(attempts: list[dict]) -> dict:
    """Sum reported numeric fields only; missing fields remain absent, never zero-filled.

    ``None`` and nested provider detail (e.g. ``prompt_tokens_details``) are not
    reported counts and are left out rather than added.
    """
    total: dict = {}
    for attempt in attempts:
        for name, value in attempt["usage"].items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                total[name] = total.get(name, 0) + value
    return total


def attach_usage(error: Exception, attempts: list[dict]) -> None:
    """Retain the original exception type and its complete logical-call usage."""
    error.attempt_usage = attempts
    error.usage = sum_usage(attempts)


def usage_metadata(value: Any, *, failed: bool = False) -> dict:
    """Add attempt provenance and unknown coverage without changing legacy payloads."""
    if not failed and not getattr(value, "attempt_usage", None):
        return {}
    attempts = attempts_for(value, "failed" if failed else "success")
    fields = set(USAGE_FIELDS).union(*(a["usage"] for a in attempts))
    unknown = sorted(name for name in fields if any(name not in a["usage"] for a in attempts))
    return {"attempt_usage": attempts, "usage_unknown": unknown}
