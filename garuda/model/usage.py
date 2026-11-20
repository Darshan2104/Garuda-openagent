"""Reported usage subtotals and coverage for one logical inference call."""

import asyncio
from typing import Any

from garuda.model.protocol import ContextOverflowError

TOKEN_FIELDS = ("prompt_tokens", "completion_tokens", "total_tokens")
USAGE_FIELDS = (*TOKEN_FIELDS, "cost_usd")


def unreported_attempts(attempts: list[dict], *, fields: tuple[str, ...] = USAGE_FIELDS) -> int:
    """Count attempts without reported usage, excluding definite failed rejections."""
    missing = 0
    for attempt in attempts:
        if (not isinstance(attempt, dict) or (
                attempt.get("outcome") == "failed" and attempt.get("rejected") is True)):
            continue
        usage = attempt.get("usage")
        if not isinstance(usage, dict) or not any(
            isinstance(usage.get(key), (int, float)) and not isinstance(usage[key], bool)
            for key in fields
        ):
            missing += 1
    return missing


def reported_prompt_tokens(attempts: list[dict]) -> int | None:
    """Largest positive reported input size for this call's repeated request."""
    values = [
        usage["prompt_tokens"] for attempt in attempts if isinstance(attempt, dict)
        and isinstance(usage := attempt.get("usage"), dict)
        and isinstance(usage.get("prompt_tokens"), (int, float))
        and not isinstance(usage["prompt_tokens"], bool) and usage["prompt_tokens"] > 0
    ]
    return int(max(values)) if values else None


def is_definite_rejection(error: BaseException | None) -> bool:
    """Whether an error proves the provider rejected this attempt."""
    status_code = getattr(error, "status_code", None)
    return isinstance(error, ContextOverflowError) or (
        isinstance(status_code, int) and not isinstance(status_code, bool)
        and 400 <= status_code <= 499 and status_code != 408)


def usage_attempt(usage: dict, outcome: str) -> dict:
    return {"usage": dict(usage), "outcome": outcome}


def attempts_for(value: Any, outcome: str) -> list[dict]:
    attempts = getattr(value, "attempt_usage", None)
    if attempts:
        return attempts
    usage = getattr(value, "usage", None)
    attempt = usage_attempt(usage if isinstance(usage, dict) else {}, outcome)
    if outcome == "failed":
        attempt["rejected"] = is_definite_rejection(value)
    return [attempt]


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


def attach_usage(error: BaseException, attempts: list[dict]) -> None:
    """Retain the original exception type and its complete logical-call usage."""
    error.attempt_usage = attempts
    error.usage = sum_usage(attempts)


def usage_metadata(value: Any, *, failed: bool = False) -> dict:
    """Add attempt provenance and unknown coverage without changing legacy payloads."""
    if not failed and not getattr(value, "attempt_usage", None):
        return {}
    outcome = "failed" if failed else "success"
    if isinstance(value, asyncio.CancelledError):
        outcome = "interrupted"
    attempts = attempts_for(value, outcome)
    fields = set(USAGE_FIELDS).union(*(a["usage"] for a in attempts))
    unknown = {name for name in fields if any(name not in a["usage"] for a in attempts)}
    if attempts[-1].get("outcome") == "interrupted":
        # Early stream usage is only a lower bound, even when every field was reported.
        unknown.update(("completion_tokens", "total_tokens", "cost_usd"))
    return {"attempt_usage": attempts, "usage_unknown": sorted(unknown)}
