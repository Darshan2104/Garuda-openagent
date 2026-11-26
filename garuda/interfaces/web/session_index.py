"""The dashboard's session listing: cached, bounded, and tolerant of bad records.

Every list view (runs, sessions, inbox, config's recent models) starts from
``SessionStore.list_sessions``, which reads and parses every ``meta.json`` in the store
on every call. At a few thousand sessions that is half a second per request, and the
nav badge alone asks every 20 seconds. :class:`CachedSessionStore` answers the same call
from memory and re-reads only what changed.

**What makes a cached entry current.** Each ``meta.json`` is keyed on its inode, size,
``mtime_ns`` and ``ctime_ns``. Session writers publish with ``os.replace`` (a new inode)
and a ``chmod`` moves ``ctime``, so a changed, replaced or re-permissioned record never
matches its old key. The directory listing is keyed on the store root's own ``mtime_ns``,
which moves whenever a session directory is created or removed. The file is stat'ed
*before* it is read: a replacement landing between the two leaves new bytes under the old
key, which the next stat then misses — never old bytes under a new key, which would stick.

**What is kept.** Only records that loaded; a record that could not be read is retried
on the next request rather than remembered as broken, because a protected session that
is mid-publication reads as broken for a moment and must not stay that way. Records are
held as compact JSON bytes, not parsed objects, and parsed fresh for each response, so no
caller can mutate what another request is served. Their total is capped
(:data:`CACHE_BYTES`); beyond it the oldest sessions keep only their sort key and are
re-read when a response actually includes them. A record larger than
:data:`MAX_META_BYTES` is reported unreadable without being read.

**Concurrency.** ``http.py`` runs each connection on its own thread, so one lock guards
the cache. It is held across a refresh: two threads that both found the cache stale
would otherwise read the same files twice and race to install their results, and the
second request of a burst should be served by the first one's work.
"""

from __future__ import annotations

import json
import os
import stat
import threading
import time
from dataclasses import dataclass

from garuda.core.session_publication import META_LIMIT
from garuda.core.sessions import SessionStore, UnreadableSession, validate_session_limit

#: The strict-publication ceiling for one ``meta.json``. A larger file is not a record
#: any Garuda writer produces, and reading it on every list request is the problem.
MAX_META_BYTES = META_LIMIT

#: Upper bound on the bytes of cached records. A finished session's record is about
#: 1 KiB, so this holds tens of thousands of sessions; beyond it the oldest are re-read.
CACHE_BYTES = 32 * 1024 * 1024

#: How often a record whose stat moved during a failed read is re-read in one request.
TORN_READ_RETRIES = 3

#: How many unreadable session ids a list response names (its count is always exact).
MAX_REPORTED_IDS = 50


def _key(st: os.stat_result) -> tuple[int, int, int, int]:
    return (st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)


@dataclass
class _Entry:
    key: tuple[int, int, int, int]
    #: The sort key of a listable record; ``None`` for a malformed one.
    updated_at: str | None
    #: Why a parsed record is not listable (``list_sessions``' strict ``ValueError``).
    problem: str | None
    #: The record as compact JSON, or ``None`` once evicted by the byte budget.
    raw: bytes | None


def unreadable_report(ids: list[str]) -> dict:
    """The ``unreadable_sessions`` block every list payload carries."""
    unique = sorted(set(ids))
    return {"count": len(unique), "ids": [name[:128] for name in unique[:MAX_REPORTED_IDS]]}


class CachedSessionStore(SessionStore):
    """A :class:`SessionStore` whose ``list_sessions`` is served from a stat-keyed cache.

    Same contract as the base method, including its strict default; only the cost
    differs. Writes, ``load_meta`` and everything else are inherited unchanged.
    """

    def __init__(self, root=None, *, cache_bytes: int = CACHE_BYTES,
                 max_meta_bytes: int = MAX_META_BYTES) -> None:
        super().__init__(root)
        self._cache_bytes = max(0, cache_bytes)
        self._max_meta_bytes = max_meta_bytes
        self._root_text = os.fspath(self.root)
        self._lock = threading.Lock()
        self._entries: dict[str, _Entry] = {}
        self._names: tuple[tuple[int, int], list[str]] | None = None
        #: How many records were parsed off disk; tests assert the cache is used.
        self.reads = 0

    # --- the public contract --------------------------------------------------

    def list_sessions(self, limit: int | None = 20, *,
                      unreadable: list[str] | None = None) -> list[dict]:
        if limit is not None:
            limit = validate_session_limit(limit)
        with self._lock:
            names = self._listing()
            if names is None:
                self._entries.clear()
                return []
            bad: list[str] = []
            listable, malformed = self._refresh(names, bad)
            if malformed and unreadable is None:
                raise ValueError(self._entries[malformed[0]].problem)
            listable.sort(key=lambda name: self._entries[name].updated_at or "", reverse=True)
            selected = listable if limit is None else listable[:limit]
            metas = self._materialize(selected, bad)
            self._apply_budget(listable)
        if unreadable is not None:
            unreadable.extend(malformed)
            unreadable.extend(bad)
        return metas

    # --- the cache ------------------------------------------------------------

    def _listing(self) -> list[str] | None:
        """The store root's entries, re-listed only when the root itself changed."""
        try:
            st = os.stat(self.root)
        except FileNotFoundError:
            self._names = None
            return None
        key = (st.st_ino, st.st_mtime_ns)
        if self._names is None or self._names[0] != key:
            self._names = (key, os.listdir(self.root))
        return self._names[1]

    def _refresh(self, names: list[str], bad: list[str]) -> tuple[list[str], list[str]]:
        """Bring every entry up to date; return the listable and the malformed names."""
        listable: list[str] = []
        malformed: list[str] = []
        current: dict[str, _Entry] = {}
        for name in names:
            entry = self._current(name, bad)
            if entry is None:
                continue
            current[name] = entry
            (listable if entry.problem is None else malformed).append(name)
        # Sessions deleted from disk leave the cache here.
        self._entries = current
        return listable, malformed

    def _stat(self, name: str) -> os.stat_result | None | bool:
        """The record's stat; ``None`` when this is not a session, ``False`` if unreadable."""
        try:
            # A plain string join: pathlib costs more than the stat itself at this scale.
            st = os.stat(os.path.join(self._root_text, name, "meta.json"))
        except (FileNotFoundError, NotADirectoryError):
            return None
        except OSError:
            return False
        if not stat.S_ISREG(st.st_mode):
            return None
        if st.st_size > self._max_meta_bytes:
            return False
        return st

    def _current(self, name: str, bad: list[str]) -> _Entry | None:
        for _attempt in range(TORN_READ_RETRIES):
            st = self._stat(name)
            if st is None:
                return None
            if st is False:
                bad.append(name)
                return None
            key = _key(st)
            cached = self._entries.get(name)
            if cached is not None and cached.key == key:
                return cached
            try:
                meta = self._read(name)
            except FileNotFoundError:
                return None
            except UnreadableSession:
                # Torn by a writer that does not replace atomically, or mid-publication:
                # if the file moved while it was read, read it again rather than report it.
                again = self._stat(name)
                if again is not None and again is not False and _key(again) != key:
                    time.sleep(0.01)
                    continue
                bad.append(name)
                return None
            except ValueError as exc:
                return _Entry(key=key, updated_at=None, problem=str(exc), raw=None)
            return _Entry(key=key, updated_at=meta.get("updated_at", ""), problem=None,
                          raw=json.dumps(meta, separators=(",", ":")).encode("utf-8"))
        bad.append(name)
        return None

    def _read(self, name: str) -> dict:
        self.reads += 1
        return self.listed_meta(name)

    def _materialize(self, selected: list[str], bad: list[str]) -> list[dict]:
        metas: list[dict] = []
        for name in selected:
            entry = self._entries[name]
            if entry.raw is None:
                # Evicted by the byte budget: re-read the one record this response needs.
                try:
                    meta = self._read(name)
                except FileNotFoundError:
                    continue
                except (UnreadableSession, ValueError):
                    bad.append(name)
                    continue
                entry.raw = json.dumps(meta, separators=(",", ":")).encode("utf-8")
                metas.append(meta)
                continue
            metas.append(json.loads(entry.raw))
        return metas

    def _apply_budget(self, newest_first: list[str]) -> None:
        """Keep the newest records' bytes within the budget; the rest keep their sort key."""
        used = 0
        for name in newest_first:
            entry = self._entries[name]
            if entry.raw is None:
                continue
            used += len(entry.raw)
            if used > self._cache_bytes:
                entry.raw = None

    def cached_bytes(self) -> int:
        with self._lock:
            return sum(len(e.raw) for e in self._entries.values() if e.raw is not None)
