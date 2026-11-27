"""The dashboard's session listing: cached, bounded, tolerant of bad records, never waiting.

Every list view (runs, sessions, inbox, overview, config's recent models) starts from
``SessionStore.list_sessions``, which reads and parses every ``meta.json`` in the store
on every call. At a few thousand sessions that is half a second per request, and the
nav badge alone asks every 20 seconds. :class:`CachedSessionStore` answers the same call
from memory and re-reads only what changed.

**Only the record is cached.** What a list row derives from other files — the queue
index, pending approvals, leases and owner liveness, a flow's journal and receipts — is
read by ``read_model`` and ``overview`` on every request, exactly as before.

**What makes a cached record current.** ``load_meta`` returns the parse of ``meta.json``
and nothing else, but whether it *succeeds* for a protected session also depends on files
beside it: the publication marker, initialization stamp, head and lock in the session
directory, and the policy record in the store's registry. Every writer of a record
(``merge_meta``, ``mutate_meta``, the publication cycle) publishes ``meta.json`` with
``os.replace``, so a changed record is a new inode. The key is therefore:

* ``meta.json``'s inode, size, ``mtime_ns`` and ``ctime_ns`` (a ``chmod`` moves ``ctime``);
* the session directory's inode, ``mtime_ns`` and ``ctime_ns``, which move whenever a
  file in it is created, removed or replaced — the head, marker, stamp and lock included;
* the stat of the session's registry policy record, or its absence (skipped while the
  store has no registry at all, the common case).

The directory listing is keyed on the store root's own ``mtime_ns``. Everything is
stat'ed *before* it is read: a replacement landing between the two leaves new bytes under
the old key, which the next stat then misses — never old bytes under a new key.

**What is kept.** Only records that loaded; a record that could not be read is retried
on the next request rather than remembered as broken. Records are held as compact JSON
bytes and parsed fresh for each response, so no caller can mutate what another request
is served. Their total is capped (:data:`CACHE_BYTES`); beyond it the oldest sessions keep
only their sort key and are re-read when a response includes them. A record larger than
:data:`MAX_META_BYTES` is reported unreadable without being read.

**Never waiting on a writer.** ``load_meta`` waits up to five seconds for a protected
session's metadata lock. Listing does not: it reads with ``load_meta(wait=False)``, one
attempt at the lock, and a record that is locked serves its previous cached version, or — with none — is reported *being written* and retried on the next request.

**Concurrency.** ``http.py`` runs each connection on its own thread. The lock guards only
the cache's dictionaries and is never held across a file read, so one request's reads
cannot delay another's. Two threads that both find a record changed both read it, and the
next stat corrects whichever installed last if it was the older.
"""

from __future__ import annotations

import json
import os
import stat
import threading
import time
from dataclasses import dataclass

from garuda.core.session_publication import META_LIMIT, REGISTRY, MetadataBusy
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


class BeingWritten(str):
    """A session id in an ``unreadable`` list whose record was locked, not damaged."""


def _file_key(st: os.stat_result) -> tuple[int, int, int, int]:
    return (st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)


@dataclass
class _Entry:
    key: tuple
    #: The sort key of a listable record; ``None`` for a malformed one.
    updated_at: str | None
    #: Why a parsed record is not listable (``list_sessions``' strict ``ValueError``).
    problem: str | None
    #: The record as compact JSON, or ``None`` once evicted by the byte budget.
    raw: bytes | None


def unreadable_report(ids: list[str]) -> dict:
    """The ``unreadable_sessions`` block every list payload carries."""
    unique = sorted(set(ids))
    busy = sorted({name for name in ids if isinstance(name, BeingWritten)})
    return {"count": len(unique), "ids": [name[:128] for name in unique[:MAX_REPORTED_IDS]],
            "being_written": [name[:128] for name in busy[:MAX_REPORTED_IDS]]}


class CachedSessionStore(SessionStore):
    """A :class:`SessionStore` whose ``list_sessions`` is served from a stat-keyed cache.

    Same contract as the base method, including its strict default; only the cost
    differs, and it never waits on a writer's lock. Writes, ``load_meta`` and everything
    else are inherited unchanged.
    """

    def __init__(self, root=None, *, cache_bytes: int = CACHE_BYTES,
                 max_meta_bytes: int = MAX_META_BYTES) -> None:
        super().__init__(root)
        self._cache_bytes = max(0, cache_bytes)
        self._max_meta_bytes = max_meta_bytes
        self._root_text = os.fspath(self.root)
        self._registry_text = os.path.join(self._root_text, REGISTRY)
        self._lock = threading.Lock()
        self._entries: dict[str, _Entry] = {}
        self._names: tuple[tuple[int, int], list[str]] | None = None
        #: ``{session_id: (record key, summarize, value)}`` for :meth:`summaries`.
        self._summaries: dict[str, tuple] = {}
        #: How many records were parsed off disk; tests assert the cache is used.
        self.reads = 0

    # --- the public contract --------------------------------------------------

    def list_sessions(self, limit: int | None = 20, *,
                      unreadable: list[str] | None = None) -> list[dict]:
        if limit is not None:
            limit = validate_session_limit(limit)
        scanned = self._scan(unreadable)
        if scanned is None:
            return []
        listable, current, bad = scanned
        selected = listable if limit is None else listable[:limit]
        metas = self._materialize(selected, current, bad)
        with self._lock:
            self._apply_budget(listable, current)
        if unreadable is not None:
            unreadable.extend(bad)
        return metas

    def summaries(self, summarize, *, unreadable: list[str]) -> list[tuple[str, object]]:
        """Every listable session, newest first, as ``(session_id, summarize(meta))``.

        For a list that filters and counts over every session but shows one page of it
        (the Sessions page): ``summarize`` runs only for a record whose cache key changed
        since it last ran for that record, so a warm request parses no record at all.
        ``summarize`` must be a pure function of the record; it is memoized by identity,
        so pass the same (module-level) function each time. Use :meth:`listed` for the
        records the response then needs in full.
        """
        scanned = self._scan(unreadable)
        if scanned is None:
            return []
        listable, current, bad = scanned
        with self._lock:
            memo = dict(self._summaries)
        fresh: dict[str, tuple] = {}
        out: list[tuple[str, object]] = []
        for name in listable:
            key = current[name].key
            hit = memo.get(name)
            if hit is not None and hit[0] == key and hit[1] is summarize:
                fresh[name] = hit
                out.append((name, hit[2]))
                continue
            metas = self._materialize([name], current, bad)
            if not metas:
                continue
            value = summarize(metas[0])
            fresh[name] = (key, summarize, value)
            out.append((name, value))
        with self._lock:
            self._summaries = fresh
            self._apply_budget(listable, current)
        unreadable.extend(bad)
        return out

    def listed(self, names: list[str], *, unreadable: list[str]) -> list[dict]:
        """The records of ``names`` (from :meth:`summaries`), as ``list_sessions`` serves them."""
        with self._lock:
            current = dict(self._entries)
        bad: list[str] = []
        metas = self._materialize([n for n in names if n in current], current, bad)
        unreadable.extend(bad)
        return metas

    # --- the cache ------------------------------------------------------------

    def _scan(self, unreadable: list[str] | None):
        """Stat every session and refresh changed records: ``(listable newest first,
        entries, unreadable ids)``, or ``None`` when the store does not exist."""
        names = self._listing()
        if names is None:
            with self._lock:
                self._entries.clear()
            return None
        with self._lock:
            known = dict(self._entries)
        registry = os.path.isdir(self._registry_text)
        bad: list[str] = []
        current: dict[str, _Entry] = {}
        for name in names:
            entry = self._current(name, known.get(name), bad, registry)
            if entry is not None:
                current[name] = entry
        with self._lock:
            # Sessions deleted from disk leave the cache here.
            self._entries = current
        malformed = [name for name, entry in current.items() if entry.problem is not None]
        if malformed and unreadable is None:
            raise ValueError(current[malformed[0]].problem)
        listable = [name for name, entry in current.items() if entry.problem is None]
        listable.sort(key=lambda name: current[name].updated_at or "", reverse=True)
        if unreadable is not None:
            unreadable.extend(malformed)
        return listable, current, bad

    def _listing(self) -> list[str] | None:
        """The store root's entries, re-listed only when the root itself changed."""
        try:
            st = os.stat(self._root_text)
        except FileNotFoundError:
            with self._lock:
                self._names = None
            return None
        key = (st.st_ino, st.st_mtime_ns)
        with self._lock:
            names = self._names
        if names is None or names[0] != key:
            names = (key, os.listdir(self._root_text))
            with self._lock:
                self._names = names
        return names[1]

    def _key(self, name: str, registry: bool) -> tuple | None | bool:
        """Everything a record's readability depends on; ``None`` when this is not a
        session, ``False`` when it cannot be read."""
        # Plain string joins: pathlib costs more than the stats themselves at this scale.
        directory = os.path.join(self._root_text, name)
        try:
            meta = os.stat(os.path.join(directory, "meta.json"))
            folder = os.stat(directory)
        except (FileNotFoundError, NotADirectoryError):
            return None
        except OSError:
            return False
        if not stat.S_ISREG(meta.st_mode):
            return None
        if meta.st_size > self._max_meta_bytes:
            return False
        policy = None
        if registry:
            try:
                policy = _file_key(os.stat(os.path.join(self._registry_text, name + ".json")))
            except FileNotFoundError:
                policy = "missing"
            except OSError:
                return False
        return (_file_key(meta), folder.st_ino, folder.st_mtime_ns, folder.st_ctime_ns, policy)

    def _current(self, name: str, cached: _Entry | None, bad: list[str],
                 registry: bool) -> _Entry | None:
        for _attempt in range(TORN_READ_RETRIES):
            key = self._key(name, registry)
            if key is None:
                return None
            if key is False:
                bad.append(name)
                return None
            if cached is not None and cached.key == key:
                return cached
            try:
                meta = self._read(name)
            except FileNotFoundError:
                return None
            except MetadataBusy:
                if cached is not None and cached.raw is not None:
                    # Serve the previous version; its stale key makes the next request retry.
                    return cached
                bad.append(BeingWritten(name))
                return None
            except UnreadableSession:
                # Torn by a writer that does not replace atomically, or mid-publication:
                # if the file moved while it was read, read it again rather than report it.
                again = self._key(name, registry)
                if again not in (None, False) and again != key:
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
        """The record, validated as ``list_sessions`` admits it, without waiting on a lock."""
        with self._lock:
            self.reads += 1
        return self.listed_meta(name, wait=False)

    def _materialize(self, selected: list[str], current: dict[str, _Entry],
                     bad: list[str]) -> list[dict]:
        metas: list[dict] = []
        for name in selected:
            entry = current[name]
            raw = entry.raw
            if raw is None:
                # Evicted by the byte budget: re-read the one record this response needs.
                try:
                    meta = self._read(name)
                except FileNotFoundError:
                    continue
                except MetadataBusy:
                    bad.append(BeingWritten(name))
                    continue
                except (UnreadableSession, ValueError):
                    bad.append(name)
                    continue
                entry.raw = json.dumps(meta, separators=(",", ":")).encode("utf-8")
                metas.append(meta)
                continue
            metas.append(json.loads(raw))
        return metas

    def _apply_budget(self, newest_first: list[str], current: dict[str, _Entry]) -> None:
        """Keep the newest records' bytes within the budget; the rest keep their sort key."""
        used = 0
        for name in newest_first:
            entry = current[name]
            if entry.raw is None:
                continue
            used += len(entry.raw)
            if used > self._cache_bytes:
                entry.raw = None

    def cached_bytes(self) -> int:
        with self._lock:
            return sum(len(e.raw) for e in self._entries.values() if e.raw is not None)
