"""The durable session queue (plan task D.1, #167): the A.4 spike promoted.

One directory, one JSON document, versioned. Every mutation holds an exclusive
``fcntl.flock`` on the directory and refuses rather than run unlocked; documents
are owner-only, written to a temporary file, fsynced and renamed, and read
without following symlinks. A corrupt, symlinked or future-version document is
left in place and refused, never "repaired".

**What an entry is.** A queued session, bound to the *user* (the queue scope is
``<user>:<harness>``), the *harness*, the *session id*, and a digest of the
configuration it was queued under. A claim additionally records the **worker**
(pid, process start identity, process group, epoch), so a late write from a
superseded worker is told apart from the current one.

**Order.** FIFO within a scope: only the oldest waiting entry may claim.

**Capacity.** There is no queue-only capacity. A claim takes a slot from the
same :class:`~garuda.runtime.capacity.CapacityStore` a foreground run uses,
under the ceiling ``harnesses.<id>.max_parallel`` (or the settings ``capacity``
table), so foreground, background, SDK, flow and consult launches all count
against one limit. With no ceiling the harness is not limited.

**Ownership.** A claim is kept on proof of life and taken only on proof of
death: it is reclaimed only when its owner is *confirmed* dead (by process
identity, as the shared capacity slot is); a live owner keeps it however old it
is, and an owner whose liveness cannot be determined is quarantined once its
heartbeat is past the lease TTL (or in the future). No takeover rests on a clock,
so a moved clock can neither shorten a live owner's claim nor prolong a dead one's.
Heartbeat, release and workspace requeue match the complete recorded owner,
including process identity and epoch. Implicit heartbeat/release use only this
instance's successfully claimed owners in the claiming process; a newly opened
store or fork cannot infer that authority from the persisted document.

**Inspection** (:meth:`QueueStore.entries` and :meth:`QueueStore.snapshot`)
reads without the lock and writes nothing, including at construction. Missing
stores stay missing; existing permissions and legacy records stay untouched.
Documents written by the spike (version 1) are read and migrated to
version 2 on the first mutation; the old document is kept once as
``state.json.v1``.

POSIX only: Windows is refused at construction.
"""

from __future__ import annotations

import contextlib
import json
import os
import time
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

from garuda.runtime.capacity import (
    CapacityStore,
    CapacityUnavailable,
    Reservation,
    adopt,
    unadopt,
)
from garuda.runtime.ownership import Liveness, Owner, current_owner, owner_liveness

STATE_VERSION = 2
READABLE_VERSIONS = (1, 2)
ENTRY_FIELDS = ("id", "seq", "user", "harness", "session_id", "config_digest", "enqueued_at")


class QueueError(Exception):
    """Base error for the queue store."""


class LockUnavailable(QueueError):
    """The cross-process lock could not be taken; nothing was changed."""


class CorruptState(QueueError):
    """The state document is unreadable, malformed or from an unknown version."""


def current_user() -> str:
    return f"uid{os.getuid()}"


def scope_for(harness: str, user: str | None = None) -> str:
    return f"{user or current_user()}:{harness}"


def default_queue_root() -> Path:
    from garuda.config.agent_home import global_settings_path

    return global_settings_path().expanduser().parent / "queue"


def _harness_of(scope: str) -> str:
    return scope.rsplit(":", 1)[-1]


class QueueStore:
    """A cross-process FIFO queue bound to user, harness, session and worker."""

    def __init__(
        self,
        root: str | Path | None = None,
        *,
        lease_ttl: float = 30.0,
        liveness: Liveness = owner_liveness,
        capacity: CapacityStore | None = None,
        ceiling: Callable[[str], int | None] | None = None,
        clock: Callable[[], float] = time.time,
    ):
        if os.name != "posix":
            raise QueueError("the queue store supports POSIX hosts only")
        self.root = Path(root) if root else default_queue_root()
        self.lease_ttl = lease_ttl
        self._liveness = liveness
        self._clock = clock
        self._capacity = capacity
        self._ceiling = ceiling
        # Implicit mutation authority comes only from this instance's successful
        # claims, never from reading an owner out of the shared queue document.
        self._claimed_owners: dict[tuple[str, str], Owner] = {}
        if self.root.is_symlink():
            raise LockUnavailable(f"{self.root} is a symlink")

    # -- shared capacity (B.0) ---------------------------------------------------------

    @property
    def capacity(self) -> CapacityStore:
        if self._capacity is None:
            self._capacity = CapacityStore()
        return self._capacity

    def ceiling_for(self, harness: str) -> int | None:
        if self._ceiling is not None:
            return self._ceiling(harness)
        from garuda.runtime.capacity import configured_ceiling

        return configured_ceiling(harness)

    # -- storage -----------------------------------------------------------------------

    @property
    def _state_path(self) -> Path:
        return self.root / "state.json"

    @staticmethod
    def _empty() -> dict:
        return {"version": STATE_VERSION, "seq": 0, "scopes": {}}

    def _read(self) -> tuple[dict, bool]:
        """``(state at the current version, whether it was migrated from an older one)``."""
        from garuda.runtime.strict_store import CorruptRecord, read_document

        try:
            state = read_document(self._state_path, versions=READABLE_VERSIONS)
        except CorruptRecord as exc:
            raise CorruptState(str(exc)) from exc
        if state is None:
            return self._empty(), False
        migrated = state.get("version", 1) != STATE_VERSION
        return self._validated(self._migrate(state)), migrated

    @staticmethod
    def _migrate(state: dict) -> dict:
        if state.get("version", 1) == STATE_VERSION:
            return state
        scopes = {}
        for scope, old in (state.get("scopes") or {}).items():
            if not isinstance(old, dict):
                raise CorruptState(f"scope {scope!r} is malformed")
            # Queue-only capacity is gone: ceilings come from the shared capacity settings.
            scopes[scope] = {
                "waiting": [{"id": w["id"], "seq": w.get("seq", 0), "user": None,
                             "harness": _harness_of(scope), "session_id": None,
                             "config_digest": None, "enqueued_at": None}
                            for w in old.get("waiting", [])],
                "claims": {k: {**v, "user": None, "harness": _harness_of(scope),
                               "session_id": None, "config_digest": None,
                               "claimed_at": v.get("heartbeat")}
                           for k, v in (old.get("claims") or {}).items()},
            }
        return {"version": STATE_VERSION, "seq": int(state.get("seq", 0)), "scopes": scopes}

    @staticmethod
    def _validated(state: dict) -> dict:
        scopes = state.get("scopes")
        if not isinstance(scopes, dict) or not isinstance(state.get("seq", 0), int):
            raise CorruptState("the queue state is malformed")
        for scope, entry in scopes.items():
            waiting, claims = entry.get("waiting"), entry.get("claims")
            if (not isinstance(waiting, list) or not isinstance(claims, dict)
                    or not all(isinstance(w, dict) and isinstance(w.get("id"), str) for w in waiting)
                    or not all(isinstance(c, dict) for c in claims.values())):
                raise CorruptState(f"scope {scope!r} is malformed")
        return state

    @contextlib.contextmanager
    def _locked(self) -> Iterator[dict]:
        """Hold the exclusive lock, yield the state, write it back atomically."""
        from garuda.runtime.strict_store import StorageUnavailable, exclusive_lock, write_document

        try:
            with exclusive_lock(self.root):
                state, migrated = self._read()
                before = json.dumps(state, sort_keys=True)
                if migrated and self._state_path.exists():
                    backup = self.root / "state.json.v1"
                    if not backup.exists():
                        backup.write_bytes(self._state_path.read_bytes())
                        os.chmod(backup, 0o600)
                yield state
                if migrated or json.dumps(state, sort_keys=True) != before:
                    write_document(self._state_path, state)
        except StorageUnavailable as exc:
            raise LockUnavailable(str(exc)) from exc

    @staticmethod
    def _scope(state: dict, scope: str) -> dict:
        return state.setdefault("scopes", {}).setdefault(scope, {"waiting": [], "claims": {}})

    # -- queueing ----------------------------------------------------------------------

    def enqueue(self, scope: str, item_id: str | None = None, *, harness: str | None = None,
                user: str | None = None, session_id: str | None = None,
                config_digest: str | None = None) -> str:
        """Add an entry or retry its exact binding without changing FIFO order.

        An item id identifies one binding across this store, including running claims.
        A conflicting retry refuses; it cannot change work that was already admitted.
        """
        item_id = item_id or uuid.uuid4().hex
        binding = {"user": user or current_user(), "harness": harness or _harness_of(scope),
                   "session_id": session_id, "config_digest": config_digest}
        with self._locked() as state:
            existing = []
            for name, entry in state["scopes"].items():
                existing.extend((name, w) for w in entry["waiting"] if w["id"] == item_id)
                if item_id in entry["claims"]:
                    existing.append((name, entry["claims"][item_id]))
            if len(existing) > 1:
                raise CorruptState(f"queue item {item_id!r} has duplicate binding records; refusing")
            if existing:
                name, record = existing[0]
                if name != scope or any(record.get(k) != v for k, v in binding.items()):
                    raise QueueError(f"queue item {item_id!r} already has a different binding")
                return item_id
            state["seq"] = int(state.get("seq", 0)) + 1
            self._scope(state, scope)["waiting"].append({
                "id": item_id, "seq": state["seq"], **binding, "enqueued_at": self._clock()})
        return item_id

    def cancel(self, scope: str, item_id: str) -> bool:
        """Remove a waiting entry. True if it was waiting."""
        with self._locked() as state:
            entry = self._scope(state, scope)
            kept = [w for w in entry["waiting"] if w["id"] != item_id]
            removed = len(kept) != len(entry["waiting"])
            entry["waiting"] = kept
        return removed

    def _expire(self, scope_state: dict, now: float) -> None:
        """Drop claims whose owner is confirmed dead; quarantine unknowns after the TTL.

        Death is decided by process identity, not by the clock, exactly as the shared
        capacity slot is, so the two stores never disagree about who holds a slot and a
        moved clock can neither shorten a live owner's claim nor prolong a dead one's."""
        for claim_id, claim in list(scope_state["claims"].items()):
            verdict = self._liveness(claim.get("owner") or {})
            if verdict is False:
                del scope_state["claims"][claim_id]
            elif verdict is None:
                age = now - float(claim.get("heartbeat", 0))
                if age < 0 or age >= self.lease_ttl:
                    claim["quarantined"] = True  # unknown liveness: never taken over

    def try_claim(self, scope: str, item_id: str, owner: Owner | None = None) -> bool:
        """Claim capacity for ``item_id`` if it is next in line and a slot is free."""
        owner = owner or current_owner()
        with self._locked() as state:
            scope_state = self._scope(state, scope)
            self._expire(scope_state, self._clock())
            waiting = scope_state["waiting"]
            if not waiting or waiting[0]["id"] != item_id:
                return False
            entry = waiting[0]
            harness = entry.get("harness") or _harness_of(scope)
            ceiling = self.ceiling_for(harness)
            if ceiling is not None:
                try:
                    self.capacity.reserve(harness, item_id, ceiling, owner=owner)
                except CapacityUnavailable:
                    return False
                adopt(harness, item_id, owner)  # the run this claim starts reserves the same slot
            waiting.pop(0)
            scope_state["claims"][item_id] = {
                "owner": owner.to_dict(), "epoch": uuid.uuid4().hex,
                "heartbeat": self._clock(), "claimed_at": self._clock(),
                "user": entry.get("user"), "harness": harness,
                "session_id": entry.get("session_id"),
                "config_digest": entry.get("config_digest"),
                "reserved": ceiling is not None,
            }
        self._claimed_owners[(scope, item_id)] = owner
        return True

    def claim(self, scope: str, item_id: str, *, timeout: float = 30.0,
              owner: Owner | None = None, initial_delay: float = 0.01,
              max_delay: float = 0.25) -> bool:
        """Wait (polling with bounded backoff) until the item claims, or time out."""
        owner = owner or current_owner()
        deadline = time.monotonic() + timeout
        delay = initial_delay
        while True:
            if self.try_claim(scope, item_id, owner):
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(delay)
            delay = min(delay * 2, max_delay)

    def claim_with_workspace(self, scope: str, item_id: str,
                             acquire_workspace: Callable[[], bool], *,
                             owner: Owner | None = None) -> bool:
        """Claim capacity, then the workspace; never wait on one while holding the other.

        The workspace is acquired outside the store lock. If it is not available
        right now, the capacity claim is released and the entry goes back to the
        head of its queue, so no slot is held by a process that cannot work."""
        owner = owner or current_owner()
        if not self.try_claim(scope, item_id, owner):
            return False
        try:
            acquired = acquire_workspace()
        except BaseException:
            self._requeue(scope, item_id, owner)
            raise
        if acquired:
            return True
        self._requeue(scope, item_id, owner)
        return False

    def _give_back(self, claim: dict, item_id: str) -> None:
        if claim.get("reserved"):
            unadopt(claim["harness"], item_id)
            owner = claim.get("owner") or {}
            self.capacity.release(Reservation(
                key=claim["harness"], holder=item_id,
                owner=Owner(pid=owner.get("pid", 0), identity=owner.get("identity", ""),
                            pgid=owner.get("pgid", 0), epoch=owner.get("epoch", ""))))

    def _requeue(self, scope: str, item_id: str, owner: Owner) -> None:
        with self._locked() as state:
            scope_state = state["scopes"].get(scope)
            claim = (scope_state or {}).get("claims", {}).get(item_id)
            if claim is None or claim.get("owner") != owner.to_dict():
                return
            del scope_state["claims"][item_id]
            self._give_back(claim, item_id)
            scope_state["waiting"].insert(0, {
                "id": item_id, "seq": 0, "user": claim.get("user"),
                "harness": claim.get("harness"), "session_id": claim.get("session_id"),
                "config_digest": claim.get("config_digest"), "enqueued_at": claim.get("claimed_at")})
        if self._claimed_owners.get((scope, item_id)) == owner:
            del self._claimed_owners[(scope, item_id)]

    def heartbeat(self, scope: str, item_id: str, owner: Owner | None = None) -> bool:
        if owner is None:
            owner = self._claimed_owners.get((scope, item_id))
            if owner is None or owner.pid != os.getpid():
                return False  # a fork cannot inherit implicit authority
        with self._locked() as state:
            claim = state["scopes"].get(scope, {}).get("claims", {}).get(item_id)
            if claim is None or claim.get("owner") != owner.to_dict():
                return False
            claim["heartbeat"] = self._clock()
            return True

    def release(self, scope: str, item_id: str, owner: Owner | None = None) -> bool:
        if owner is None:
            owner = self._claimed_owners.get((scope, item_id))
            if owner is None or owner.pid != os.getpid():
                return False
        with self._locked() as state:
            claims = state["scopes"].get(scope, {}).get("claims", {})
            claim = claims.get(item_id)
            if claim is None or claim.get("owner") != owner.to_dict():
                return False
            del claims[item_id]
            self._give_back(claim, item_id)
        if self._claimed_owners.get((scope, item_id)) == owner:
            del self._claimed_owners[(scope, item_id)]
        return True

    # -- inspection (never writes) -------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        """Detached state read from the atomic document, without locking or writing.

        Historical records are projected in memory only; inspection never
        publishes migrations, creates backups or initializes a missing store.
        """
        state, _migrated = self._read()
        return state

    def entries(self, scope: str | None = None) -> list[dict]:
        """Queued and running entries with their position — read without the lock,
        writing nothing (a read of the atomically replaced document is consistent)."""
        state, _migrated = self._read()
        out = []
        for name, entry in sorted(state["scopes"].items()):
            if scope is not None and name != scope:
                continue
            for position, waiting in enumerate(entry["waiting"], start=1):
                out.append({"scope": name, "state": "queued", "position": position,
                            **{k: waiting.get(k) for k in ENTRY_FIELDS}})
            for item_id, claim in entry["claims"].items():
                out.append({"scope": name, "state": "running", "position": 0, "id": item_id,
                            "user": claim.get("user"), "harness": claim.get("harness"),
                            "session_id": claim.get("session_id"),
                            "config_digest": claim.get("config_digest"),
                            "worker": claim.get("owner"), "heartbeat": claim.get("heartbeat"),
                            "quarantined": bool(claim.get("quarantined"))})
        return out
