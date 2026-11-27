"""A running flow step's tool and approval state, for read-only readers (#856).

The ACP adapter's silence watchdog knows, in memory, whether a tool call is in
flight and whether a permission request is parked
(:meth:`garuda.acp.adapter.AcpRuntime.report_status`). A flow step that asked
for liveness limits or ``long_run`` passes a :class:`StatusWriter` for its
attempt, which persists that state beside the attempt's progress file as
``flow/progress/<step>-<attempt>.status.json``::

    {"tool_in_flight": bool, "approval_parked": bool, "updated_at": "<ISO 8601>"}

``flow show`` and the dashboard read it (:func:`read`) without holding the
step's process. The file is owner-only and replaced atomically; it is written
only when the state changes, at most once a second (a change inside that
second is written when the second is up). Observability only: a write failure
never stops a step. ``garuda run`` and the SDK never write one.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import time
from pathlib import Path

SUFFIX = ".status.json"
#: The least time between two writes of one attempt's status file.
MIN_INTERVAL_SEC = 1.0
#: Bound on a status file a reader accepts.
MAX_BYTES = 4 * 1024


def path(flow_dir, step: str, attempt: int) -> Path:
    """The attempt's status file, beside its progress file."""
    return Path(flow_dir) / "progress" / f"{step}-{attempt}{SUFFIX}"


def read(target) -> dict | None:
    """The attempt's status record, or None when it is missing, unreadable or malformed."""
    try:
        fd = os.open(target, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "rb") as handle:
            raw = handle.read(MAX_BYTES + 1)
        record = json.loads(raw) if len(raw) <= MAX_BYTES else None
    except (OSError, ValueError):
        return None
    if (not isinstance(record, dict) or type(record.get("tool_in_flight")) is not bool
            or type(record.get("approval_parked")) is not bool
            or not isinstance(record.get("updated_at"), str)):
        return None
    return {key: record[key] for key in ("tool_in_flight", "approval_parked", "updated_at")}


class StatusWriter:
    """Persists one attempt's ``(tool_in_flight, approval_parked)``; the adapter's callback.

    Called with the new state when it changes. A state equal to the last one
    written is not written again; a change less than :data:`MIN_INTERVAL_SEC`
    after the last write is held and written when the interval is up (on the
    running event loop, or at the next call without one), so the file always
    ends at the latest state.
    """

    def __init__(self, target, *, min_interval: float = MIN_INTERVAL_SEC, clock=time.monotonic):
        self.path = Path(target)
        self.min_interval = min_interval
        self._clock = clock
        self._written: tuple[bool, bool] | None = None
        self._last_write: float | None = None
        self._pending: tuple[bool, bool] | None = None
        self._timer = None
        self.writes = 0

    def __call__(self, tool_in_flight: bool, approval_parked: bool) -> None:
        state = (bool(tool_in_flight), bool(approval_parked))
        if state == self._written:
            self._pending = None
            return
        now = self._clock()
        if self._last_write is None or now - self._last_write >= self.min_interval:
            self._pending = None
            self._write(state, now)
            return
        self._pending = state
        if self._timer is None:
            try:
                import asyncio

                loop = asyncio.get_running_loop()
            except RuntimeError:
                return  # no loop: the next call writes it
            self._timer = loop.call_later(self.min_interval - (now - self._last_write), self._flush)

    def _flush(self) -> None:
        self._timer = None
        state, self._pending = self._pending, None
        if state is not None and state != self._written:
            self._write(state, self._clock())

    def _write(self, state: tuple[bool, bool], now: float) -> None:
        self._last_write = now
        record = {"tool_in_flight": state[0], "approval_parked": state[1],
                  "updated_at": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")}
        temporary = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        try:
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC
                         | getattr(os, "O_NOFOLLOW", 0), 0o600)
            try:
                os.write(fd, json.dumps(record, sort_keys=True).encode())
            finally:
                os.close(fd)
            os.replace(temporary, self.path)
        except OSError:
            return  # observability only: never stop a step on its status file
        self._written = state
        self.writes += 1
