"""Process-wide concurrency governor for model API calls.

N parallel runs sharing one process (a job-queue server or batch eval) each
create their own model client and call the same provider. Without a cap they can
blow past the provider's requests-per-minute limit and waste tokens/time on 429
retries. This governor limits concurrent in-flight calls **per provider** so the
fleet self-throttles.

Default is unlimited (limit ``<= 0``) — single-run behavior is unchanged. Set a
limit via the ``GARUDA_MODEL_MAX_CONCURRENCY`` env var or :func:`set_max_concurrency`.
Providers are bucketed by the model-name prefix (``fireworks_ai`` in
``fireworks_ai/accounts/...``), so distinct providers don't block each other.
"""

from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager

ENV_MAX_CONCURRENCY = "GARUDA_MODEL_MAX_CONCURRENCY"


def provider_of(model_name: str) -> str:
    """The provider bucket key: the part before the first ``/`` (or the whole name)."""
    return model_name.split("/", 1)[0] if model_name else ""


class ModelGovernor:
    """Caps concurrent model calls per provider. ``limit <= 0`` means unlimited."""

    def __init__(self, limit: int = 0):
        self._limit = limit
        self._sems: dict[str, asyncio.Semaphore] = {}

    @property
    def limit(self) -> int:
        return self._limit

    def set_limit(self, limit: int) -> None:
        # Drop existing semaphores so the new limit takes effect for later calls.
        self._limit = limit
        self._sems = {}

    @asynccontextmanager
    async def slot(self, provider: str):
        """Hold one concurrency slot for ``provider`` for the duration of the block."""
        if self._limit <= 0:
            yield
            return
        # get→create is safe without a lock: no await between the check and the
        # insert, so no other coroutine interleaves (asyncio is cooperative).
        sem = self._sems.get(provider)
        if sem is None:
            sem = asyncio.Semaphore(self._limit)
            self._sems[provider] = sem
        async with sem:
            yield


_governor: ModelGovernor | None = None


def _initial_limit() -> int:
    raw = os.environ.get(ENV_MAX_CONCURRENCY, "").strip()
    if not raw:
        return 0
    try:
        return max(0, int(raw))
    except ValueError:
        return 0


def get_governor() -> ModelGovernor:
    """The process-wide governor singleton (created from the env on first use)."""
    global _governor
    if _governor is None:
        _governor = ModelGovernor(_initial_limit())
    return _governor


def set_max_concurrency(limit: int) -> None:
    """Set the per-provider concurrent-call limit (``<= 0`` = unlimited)."""
    get_governor().set_limit(limit)
