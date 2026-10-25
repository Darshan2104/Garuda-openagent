"""Strict quota evidence for automatic recovery decisions."""

from dataclasses import replace

import pytest

from garuda.observability.limits import (
    LimitObservation,
    Window,
    recovery_availability,
    recovery_exhausted,
)

NOW = 1_790_000_000.0
ACCOUNT = "acct:0123456789abcdef"
VERSION = "0.163.0"


def observation(
    *,
    ordinary_usage_allowed=True,
    reached=False,
    primary=0.2,
    secondary=0.09,
    observed_at=NOW,
) -> LimitObservation:
    windows = (
        Window("primary", primary, NOW + 3600, 300),
        Window("secondary", secondary, NOW + 86400, 10080),
    )
    exhausted_resets = [window.resets_at for window in windows
                        if window.used_fraction is not None and window.used_fraction >= 1.0]
    return LimitObservation(
        harness="codex",
        harness_version=VERSION,
        observed_at=observed_at,
        source="codex.account.rateLimits.read",
        account_digest=ACCOUNT,
        windows=windows,
        limit_id="codex",
        reached=reached,
        reset_at=min(exhausted_resets) if reached and exhausted_resets else None,
        ordinary_usage_allowed=ordinary_usage_allowed,
    )


def availability(current, *, account_digest=ACCOUNT, harness_version=VERSION, now=NOW):
    return recovery_availability(
        current,
        account_digest=account_digest,
        harness_version=harness_version,
        now=now,
    )


def test_recovery_needs_fresh_affirmative_availability_for_the_same_identity():
    assert availability(observation()) == (
        True,
        "fresh quota observation affirmatively allows ordinary usage",
    )

    available, reason = availability(observation(), account_digest="acct:changed")
    assert available is False and "account changed" in reason

    available, reason = availability(observation(), harness_version="0.159.3")
    assert available is False and "version changed" in reason


def test_a_weekly_exhaustion_blocks_recovery_and_proves_exhaustion():
    current = observation(
        ordinary_usage_allowed=False,
        reached=True,
        primary=0.2,
        secondary=1.0,
    )

    available, availability_reason = availability(current)
    exhausted, exhaustion_reason = recovery_exhausted(current, now=NOW)

    assert available is False and "secondary" in availability_reason
    assert exhausted is True and "future reset" in exhaustion_reason


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"ordinary_usage_allowed": None}, "ordinary usage availability was not reported"),
        ({"observed_at": NOW - 61}, "stale"),
        ({"observed_at": NOW + 1}, "future"),
        ({"observed_at": float("nan")}, "malformed observation time"),
        ({"windows": (Window("primary", float("inf"), NOW + 1, 300),)},
         "malformed used fraction"),
        ({"windows": (Window("primary", 1.01, NOW + 1, 300),)},
         "out-of-range used fraction"),
        ({"windows": (Window("primary", 0.2, float("inf"), 300),)},
         "malformed reset"),
        ({"windows": (Window("primary", 0.2, 10**400, 300),)},
         "malformed reset"),
    ],
)
def test_malformed_or_unavailable_evidence_cannot_recover(change, reason):
    current = replace(observation(), **change)
    available, actual_reason = availability(current)
    exhausted, exhaustion_reason = recovery_exhausted(current, now=NOW)

    assert available is False
    assert reason in actual_reason
    assert exhausted is False
    assert reason in exhaustion_reason


def test_exhaustion_requires_an_explicit_denial_and_unexpired_reset():
    exhausted = observation(
        ordinary_usage_allowed=False,
        reached=True,
        primary=1.0,
    )
    assert recovery_exhausted(exhausted, now=NOW)[0] is True

    for current, reason in (
        (replace(exhausted, ordinary_usage_allowed=None),
         "ordinary usage availability was not reported"),
        (replace(exhausted, ordinary_usage_allowed=True), "ordinary usage is allowed"),
        (replace(exhausted, reset_at=NOW - 1,
                 windows=(Window("primary", 1.0, NOW - 1, 300),)), "reset has passed"),
    ):
        is_exhausted, actual_reason = recovery_exhausted(current, now=NOW)
        assert is_exhausted is False
        assert reason in actual_reason


def test_unproved_version_or_source_cannot_drive_automatic_recovery():
    for current in (
        replace(observation(), harness_version="0.163.1"),
        replace(observation(), source="some.other.source"),
    ):
        assert availability(current)[0] is False
        assert recovery_exhausted(current, now=NOW)[0] is False
