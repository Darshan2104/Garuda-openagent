"""Keeping one bad session record from taking a dashboard response down with it.

The shared read model (``core/read_model.py``) and the store list sessions strictly: a
record that parses but is malformed raises for the whole list, and a field of the wrong
type raises from deep inside a projection. Both are right for a caller that selects by
order or must refuse on damaged evidence, and wrong for a page that shows everything:
there, one damaged directory made the runs, sessions, inbox and config routes all fail.

These helpers keep the read model's rows as they are and change only how a failure is
contained: a list skips and *counts* what it could not read (the UI says how many), and
a single session's damaged record is a typed ``session_unreadable`` error, never a 500.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from garuda.interfaces.web.wire import BadRequest, Response, error, not_found

logger = logging.getLogger(__name__)

CODE_SESSION_UNREADABLE = "session_unreadable"
CODE_SESSION_BUSY = "session_being_written"


class ListedStore:
    """``ctx.store`` as the shared read model sees it from a list route.

    ``read_model`` lists with the store's strict default, under which one malformed
    record raises for the whole list. Here the listing is the dashboard's own tolerant
    one: ``metas`` already read (so a route can rebuild rows one at a time), or a live
    listing that skips unreadable sessions and records them in ``unreadable``.
    Everything else is the real store.
    """

    def __init__(self, store, metas: list[dict] | None = None) -> None:
        self._store = store
        self._metas = metas
        self.unreadable: list[str] = []

    def list_sessions(self, limit: int | None = 20, **_ignored) -> list[dict]:
        if self._metas is not None:
            return list(self._metas)
        return self._store.list_sessions(limit, unreadable=self.unreadable)

    def __getattr__(self, name: str):
        return getattr(self._store, name)


class QueueOnce:
    """One read of the queue shared by every row of a response, failure included."""

    def __init__(self, queue) -> None:
        self._queue = queue
        self._result: tuple[bool, Any] | None = None

    def entries(self):
        if self._result is None:
            try:
                self._result = (True, self._queue.entries())
            except Exception as exc:  # read_model reports an unreadable queue as unknown
                self._result = (False, exc)
        succeeded, value = self._result
        if not succeeded:
            raise value
        return value

    def __getattr__(self, name: str):
        return getattr(self._queue, name)


def rows_isolating(build: Callable[[list[dict]], list], metas: list[dict],
                   unreadable: list[str], *, newest_last: bool = False) -> list:
    """``build(metas)``, or — when one record breaks the projection — every row but its.

    The batch is tried first, so the common case costs one pass. On a failure each
    session is projected alone and the ones that still fail are reported unreadable
    (and logged) instead of taking every other row down with them.
    """
    try:
        return build(metas)
    except Exception:
        logger.warning("Dashboard list projection failed; isolating the bad session(s)",
                       exc_info=True)
    rows: list = []
    for meta in (reversed(metas) if newest_last else metas):
        try:
            rows.extend(build([meta]))
        except Exception:
            name = meta.get("session_id")
            logger.exception("Dashboard could not project session %r", name)
            unreadable.append(name if isinstance(name, str) else "?")
    return rows


def unreadable_session(session_id: str, detail: str) -> Response:
    return error(CODE_SESSION_UNREADABLE,
                 f"Session {session_id!r} has a record the dashboard cannot read ({detail}). "
                 "The rest of the dashboard is unaffected.", status=409)


def meta_refusal(store, session_id: str) -> Response | None:
    """``None`` when the session's metadata is a readable record, else the error to send.

    A writer that does not publish atomically can be caught mid-write: when the file
    moved while it was read, it is read again, and one that keeps moving is reported as
    being written (503, retry) rather than as broken.
    """
    import time as _time

    from garuda.core.sessions import UnreadableSession

    path = store.session_dir(session_id) / "meta.json"

    def stamp():
        try:
            st = path.stat()
        except OSError:
            return None
        return (st.st_ino, st.st_size, st.st_mtime_ns)

    for _attempt in range(3):
        before = stamp()
        try:
            store.listed_meta(session_id)
            return None
        except FileNotFoundError:
            return not_found(f"No session {session_id!r}.")
        except (UnreadableSession, ValueError) as exc:
            if stamp() == before:
                return unreadable_session(session_id, str(exc))
        _time.sleep(0.05)
    response = error(CODE_SESSION_BUSY, f"Session {session_id!r} is being written; retry.",
                     status=503)
    response.headers["Retry-After"] = "1"
    return response


def projected(session_id: str, build: Callable[[], Response]) -> Response:
    """One session's projection; a record that breaks it is a typed 409, not a 500."""
    from garuda.runtime.strict_store import StorageError

    try:
        return build()
    except BadRequest:
        raise
    except FileNotFoundError:
        return not_found(f"No session {session_id!r}.")
    except (TypeError, KeyError, AttributeError, IndexError, ValueError, StorageError) as exc:
        logger.warning("Dashboard could not project session %r", session_id, exc_info=True)
        return unreadable_session(session_id, type(exc).__name__)
