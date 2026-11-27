"""One harness discovery shared by the dashboard's runtime pages for a short while.

``discover`` runs every configured harness's version and status probes — subprocesses,
about a second and a half on a machine with real harnesses. The runtimes board, a
runtime's detail, the providers page and (through its own cache) setup each ran it on
every request, and providers ran it and then rebuilt the registry again.
:class:`DiscoveryMemo` lets them share one result for :data:`DISCOVERY_TTL_SECONDS`.

It lives on the :class:`~garuda.interfaces.web.routes.DashboardContext`, not in a module
global, so two dashboards (or two tests) never see each other's probes. One slot, keyed
on the registry's manifests and disabled set: a configuration change is a miss, and
memory is one discovery. The lock is held while probing, so a burst of page loads waits
for one round of probes instead of each starting its own — and only runtime pages wait:
no other route takes this lock.
"""

from __future__ import annotations

import threading
import time
from typing import Callable

from garuda.acp.catalog import discover

#: Long enough that moving between the runtime pages never re-probes, short enough that
#: a harness installed or upgraded in another terminal shows up without a restart. The
#: Providers Refresh discards it.
DISCOVERY_TTL_SECONDS = 30.0


class DiscoveryMemo:
    def __init__(self, ttl: float = DISCOVERY_TTL_SECONDS,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.ttl = ttl
        self._clock = clock
        self._lock = threading.Lock()
        self._slot: tuple[str, float, list] | None = None
        #: How many full discoveries ran; tests assert the memo is used.
        self.probes = 0

    @staticmethod
    def _key(registry) -> str:
        return repr((tuple(registry.manifests), sorted(registry.disabled_ids)))

    def peek(self, registry) -> list | None:
        """The current discovery for ``registry``, or ``None``. Never probes."""
        with self._lock:
            return self._fresh(self._key(registry))

    def get(self, registry) -> list:
        """The current discovery for ``registry``, probing every harness if there is none."""
        key = self._key(registry)
        with self._lock:
            found = self._fresh(key)
            if found is None:
                self.probes += 1
                found = discover(registry.manifests, disabled=registry.disabled_ids)
                self._slot = (key, self._clock(), found)
            return found

    def forget(self) -> None:
        """Make the next read probe again (the Providers Refresh)."""
        with self._lock:
            self._slot = None

    def _fresh(self, key: str) -> list | None:
        if self._slot is None:
            return None
        slot_key, at, found = self._slot
        if slot_key != key or not 0 <= self._clock() - at < self.ttl:
            return None
        return found
