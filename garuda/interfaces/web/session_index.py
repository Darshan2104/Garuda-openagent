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

**Racily clean stats.** The kernel stamps times from a coarse clock, so a second change
in the same tick as the read before it leaves every stat unchanged: a session directory
created just after a listing, or a ``meta.json`` rewritten in place with the same size.
As in git's racy-index rule, an unchanged key is trusted only when the times it carries
are :data:`RACY_NS` older than the moment it was taken. Until then the root is listed
again on every request, and a record is checked against a digest of the bytes it was
read from (plus its directory's entries) — a raw read, not a parse, so ``reads`` does not
move unless the record really changed. A warm request against a store whose root and
records are older than that does no listing, no read and no parse.

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

import hashlib
import json
import os
import stat
import threading
import time
from dataclasses import dataclass, field, replace

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

#: How much older than the moment it was taken a stat's times must be before an unchanged
#: stat is trusted. Covers the coarse clock the kernel stamps times from (a tick is a few
#: milliseconds) and filesystems with coarser timestamps, with room to spare.
RACY_NS = 2_000_000_000


class BeingWritten(str):
    """A session id in an ``unreadable`` list whose record was locked, not damaged."""


def _file_key(st: os.stat_result) -> tuple[int, int, int, int]:
    return (st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)


def _stamp(key: tuple) -> int:
    """The newest time in a record key: ``meta.json``'s and its directory's."""
    meta, _ino, folder_mtime, folder_ctime, _policy = key
    return max(meta[2], meta[3], folder_mtime, folder_ctime)


@dataclass
class _Entry:
    key: tuple
    #: The sort key of a listable record; ``None`` for a malformed one.
    updated_at: str | None
    #: Why a parsed record is not listable (``list_sessions``' strict ``ValueError``).
    problem: str | None
    #: The record as compact JSON, or ``None`` once evicted by the byte budget.
    raw: bytes | None
    #: ``time.time_ns()`` just before ``key`` was last stat'ed and found current.
    checked_at: int = 0
    #: While racily clean, what the record was read from (see :meth:`_proof`).
    proof: tuple | None = None
    #: Identifies the parse this entry holds; kept when a racy entry is confirmed.
    token: object = field(default_factory=object)

    def trusted(self) -> bool:
        """Whether an unchanged stat proves this record unchanged (it is not racy)."""
        return _stamp(self.key) < self.checked_at - RACY_NS


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
        #: ``(root key, trusted, names)``; see :meth:`_listing`.
        self._names: tuple[tuple[int, int], bool, list[str]] | None = None
        #: ``{session_id: (entry token, summarize, value)}`` for :meth:`summaries`.
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
            # The token, not the stat key: a racy record re-read under an unchanged key
            # is a new parse, and a confirmed one keeps its token.
            key = current[name].token
            hit = memo.get(name)
            if hit is not None and hit[0] is key and hit[1] is summarize:
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
        """The store root's entries, re-listed unless the root is unchanged *and* its
        mtime was safely older than the previous listing (git's racy-index rule).

        A directory's mtime comes from the kernel's coarse clock, so a session created in
        the same tick as the previous listing leaves ``(st_ino, st_mtime_ns)`` unchanged.
        A listing taken while the root's mtime is within :data:`RACY_NS` of it is not
        trusted; the root is listed again until a listing postdates it by that much."""
        # Taken before the stat: anything created after it is stamped at least this late,
        # less one clock tick, which RACY_NS covers.
        now = time.time_ns()
        try:
            st = os.stat(self._root_text)
        except FileNotFoundError:
            with self._lock:
                self._names = None
            return None
        key = (st.st_ino, st.st_mtime_ns)
        with self._lock:
            names = self._names
        if names is None or names[0] != key or not names[1]:
            names = (key, st.st_mtime_ns < now - RACY_NS, os.listdir(self._root_text))
            with self._lock:
                self._names = names
        return names[2]

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
            # Taken before the stat, as in :meth:`_listing`.
            now = time.time_ns()
            key = self._key(name, registry)
            if key is None:
                return None
            if key is False:
                bad.append(name)
                return None
            racy = _stamp(key) >= now - RACY_NS
            checked = None
            if cached is not None and cached.key == key:
                if cached.trusted():
                    return cached
                # Racily clean: the same stat may hide a change made in the tick it was
                # taken. Compare what the record was read from; only a difference re-reads.
                checked = self._proof(name)
                if checked is not None and checked == cached.proof:
                    return replace(cached, checked_at=now, proof=checked if racy else None)
            # Before the parse: a change between the two makes the next check re-read.
            proof = (checked or self._proof(name)) if racy else None
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
                return _Entry(key=key, updated_at=None, problem=str(exc), raw=None,
                              checked_at=now, proof=proof)
            return _Entry(key=key, updated_at=meta.get("updated_at", ""), problem=None,
                          raw=json.dumps(meta, separators=(",", ":")).encode("utf-8"),
                          checked_at=now, proof=proof)
        bad.append(name)
        return None

    def _proof(self, name: str) -> tuple | None:
        """A digest of ``meta.json``'s bytes and the session directory's entry names, read
        without parsing or locking; ``None`` when either cannot be read."""
        directory = os.path.join(self._root_text, name)
        try:
            fd = os.open(os.path.join(directory, "meta.json"), os.O_RDONLY | os.O_NONBLOCK)
        except OSError:
            return None
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                return None
            digest = hashlib.sha256()
            left = self._max_meta_bytes + 1
            while left > 0:
                chunk = os.read(fd, min(left, 1 << 16))
                if not chunk:
                    break
                digest.update(chunk)
                left -= len(chunk)
            return digest.digest(), tuple(sorted(os.listdir(directory)))
        except OSError:
            return None
        finally:
            os.close(fd)

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
