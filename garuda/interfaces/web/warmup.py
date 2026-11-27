"""Fill the dashboard's caches in the background at start, so the first page paints fast.

The first request after ``garuda web`` starts used to pay for everything at once: the
session cache's first read of every ``meta.json`` (``session_index.CachedSessionStore``),
the Sessions list's per-record summaries, harness discovery (``discovery.DiscoveryMemo``)
and the cold imports behind Setup — seconds on a real store, while the page sat blank.

:func:`warm` runs the same reads a page load runs, in the same order a person opening the
Overview triggers them, on a daemon thread, after the port is bound (startup is not
delayed). It writes nothing and starts nothing beyond what that page load runs: in
``--read-only`` too it runs exactly the probes the Overview's own requests would (the
providers read through the shared discovery memo, the setup read). Each step checks the
stop event first, so shutdown never waits on more than the step in progress; a failure is
logged at debug level and the request path then simply does the work itself.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable

logger = logging.getLogger(__name__)

#: How long shutdown waits for the step in progress before leaving the daemon thread.
JOIN_SECONDS = 2.0


def warm(ctx, stop: threading.Event) -> dict[str, float]:
    """Run each cache-filling read once, in order; returns seconds per completed step."""
    from garuda.interfaces.web import reads
    from garuda.interfaces.web.runtimes import list_runtimes, registry_from_context

    workspace = str(ctx.workspace)
    steps: list[tuple[str, Callable[[], object]]] = [
        # Every record into the session cache: what the Overview's first read needs.
        ("sessions", lambda: ctx.store.list_sessions(limit=None, unreadable=[])),
        # The providers read's discovery, shared through the memo by every runtime page.
        ("discovery", lambda: list_runtimes(registry=registry_from_context(ctx.extra, workspace),
                                            memo=ctx.discovery)),
        # The Sessions page's memoized per-record summaries (search text, state).
        ("summaries", lambda: reads.session_page(ctx.store, limit=reads.DEFAULT_LIMIT)),
        ("setup", lambda: _setup(workspace)),
    ]
    timings: dict[str, float] = {}
    for name, step in steps:
        if stop.is_set():
            break
        started = time.monotonic()
        try:
            step()
        except Exception:  # noqa: BLE001 - a warm-up miss only means the request does it
            logger.debug("Dashboard warm-up step %s failed", name, exc_info=True)
            continue
        timings[name] = time.monotonic() - started
    return timings


def _setup(workspace: str) -> None:
    from garuda.core import setup_view

    setup_view.setup(workspace)


class Warmer:
    """The warm-up thread: :meth:`start` after binding, :meth:`stop` on shutdown."""

    def __init__(self, ctx) -> None:
        self._ctx = ctx
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.timings: dict[str, float] = {}

    def start(self) -> None:
        def run() -> None:
            self.timings = warm(self._ctx, self._stop)

        self._thread = threading.Thread(target=run, name="garuda-web-warmup", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=JOIN_SECONDS)
