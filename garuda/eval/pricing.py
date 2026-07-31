"""A versioned, in-repo snapshot of model prices.

Benchmark economics have to be reproducible: the same trajectory must price the
same way next month as it does today, on any machine, offline. litellm's table is
not that and was never meant to be. It is **fetched over the network from GitHub
at import time** (unless ``LITELLM_LOCAL_MODEL_COST_MAP`` is set), it is edited
continuously upstream, and a version bump can add, remove, or re-rate a model
with no change on this side. A cost figure that moves on its own is not a
measurement, and a test that asserts on one is not a test.

So pricing resolves against the snapshot below before it ever asks litellm.
litellm stays as the last-resort estimate for models the snapshot does not name —
useful, but pinned to its *bundled* table (see ``costs``) so at worst it is a
function of the installed version rather than of the network.

Updating: edit the rates, bump :data:`SNAPSHOT_VERSION`, and note where the
numbers came from. Deliberately manual — a price change should appear in a diff
and be reviewed like any other change to how results are computed.
"""

from __future__ import annotations

# Bump on every rate change. Exported into ATIF metrics so a cost figure can be
# traced back to the table that produced it.
SNAPSHOT_VERSION = "2026-07-31"

# Keys match as *substrings* of the model name, longest match winning — the same
# convention as ``GARUDA_TOKEN_PRICES`` — so "minimax-m2.5" covers
# "openrouter/minimax/minimax-m2.5" without enumerating every provider prefix.
#
# Rates are USD per token. ``cache_read``/``cache_write`` fall back to ``input``
# when a provider does not price them separately.
#
# Source: litellm 1.87.0's bundled model_prices_and_context_window_backup.json,
# read on 2026-07-31. Verified against provider pricing pages where they publish
# one. Where a run has measured its own effective rates, pin them per-run with
# GARUDA_TOKEN_PRICES rather than editing this table — the snapshot is meant to
# be the published list price, not one account's negotiated one.
SNAPSHOT: dict[str, dict[str, float]] = {
    "minimax-m2.5": {
        "input": 3e-07,
        "cache_read": 1.5e-07,
        "output": 1.1e-06,
    },
    "claude-sonnet-4-20250514": {
        "input": 3e-06,
        "cache_read": 3e-07,
        "cache_write": 3.75e-06,
        "output": 1.5e-05,
    },
    "gpt-4o-mini": {
        "input": 1.5e-07,
        "cache_read": 7.5e-08,
        "output": 6e-07,
    },
    "o4-mini": {
        "input": 1.1e-06,
        "cache_read": 2.75e-07,
        "output": 4.4e-06,
    },
    "gemini-2.5-flash": {
        "input": 3e-07,
        "cache_read": 3e-08,
        "output": 2.5e-06,
    },
    "gpt-oss-120b": {
        "input": 1.5e-07,
        "output": 6e-07,
    },
}


def snapshot_rates(model_name: str) -> dict[str, float] | None:
    """Per-token rates for ``model_name`` from the snapshot, or None.

    Longest matching key wins, so a specific entry always beats a family-wide
    one — "minimax-m2.5" over a hypothetical "minimax".
    """
    if not model_name:
        return None
    matches = [key for key in SNAPSHOT if key and key in model_name]
    if not matches:
        return None
    return SNAPSHOT[max(matches, key=len)]
