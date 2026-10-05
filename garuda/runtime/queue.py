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

**Ownership.** Pre-activation claims may be removed only on confirmed owner
death. Activated claims stay quarantined after owner death because runtime
descendants may still exist. A live owner keeps its claim however old it is;
unknown liveness is quarantined after the lease TTL or a future heartbeat.
TTL never grants takeover authority. Queue version 3 journals intent before
reserving protected capacity (version 2), commits selection before adoption,
and publishes claim removal before returning capacity. Ordinary capacity
callers cannot reclaim queue slots, even after confirmed owner death.
Heartbeat, release and workspace requeue match the complete recorded owner,
including process identity and epoch. Implicit heartbeat/release use only this
instance's successfully claimed owners in the claiming process; a newly opened
store or fork cannot infer that authority from the persisted document.

**Inspection** (:meth:`QueueStore.entries` and :meth:`QueueStore.snapshot`)
reads without the lock and writes nothing, including at construction. Missing
stores stay missing; existing permissions and legacy records stay untouched.
Older records remain readable for diagnosis. Version 1 work and version 2
claims refuse migration because their binding or activation evidence is missing.
Empty records and fully bound, ordered version 2 waiters migrate after exact
durable ``state.json.v1`` or ``state.json.v2`` backups, before version 3 publication.
Ambiguous archives refuse and stay intact.
Legacy capacity never becomes a shared capacity setting.

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

from garuda.runtime import queue_legacy
from garuda.runtime.capacity import (
    CapacityStore,
    adopt,
)
from garuda.runtime.ownership import Liveness, Owner, current_owner, owner_liveness
from garuda.runtime.queue_journal import JournalError, QueueJournal
from garuda.runtime.queue_slots import check_scope_binding

STATE_VERSION = 3
READABLE_VERSIONS = (1, 2, 3)
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
        return {"version": STATE_VERSION, "seq": 0, "scopes": {}, "pending": {}}

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
        if state.get("version", 1) in (2, STATE_VERSION):
            return state
        try:
            return queue_legacy.project(state)
        except queue_legacy.LegacyQueueError as exc:
            raise CorruptState(str(exc)) from exc

    def _validated(self, state: dict) -> dict:
        scopes = state.get("scopes")
        if (not isinstance(scopes, dict) or type(state.get("seq")) is not int or state["seq"] < 0
                or type(state.get("version")) is not int):
            raise CorruptState("the queue state is malformed")
        seen = set()
        for scope, entry in scopes.items():
            if not isinstance(scope, str) or not isinstance(entry, dict):
                raise CorruptState("the queue scope is malformed")
            waiting, claims = entry.get("waiting"), entry.get("claims")
            if (not isinstance(waiting, list) or not isinstance(claims, dict)
                    or not all(isinstance(w, dict) and isinstance(w.get("id"), str) for w in waiting)
                    or not all(isinstance(c, dict) for c in claims.values())):
                raise CorruptState(f"scope {scope!r} is malformed")
            for item_id in [w["id"] for w in waiting] + list(claims):
                if item_id in seen:
                    raise CorruptState(f"queue item {item_id!r} has duplicate binding records; refusing")
                seen.add(item_id)
            if state["version"] == STATE_VERSION:
                if any(not all(isinstance(w.get(k), str) and w[k] for k in ("id", "user", "harness"))
                       or any(w.get(k) is not None and (not isinstance(w[k], str) or not w[k])
                              for k in ("session_id", "config_digest")) for w in waiting):
                    raise CorruptState("waiting record has a malformed admission binding")
                sequence = [w.get("seq") for w in waiting]
                if (any(type(seq) is not int or not 1 <= seq <= state["seq"] for seq in sequence)
                        or any(a >= b for a, b in zip(sequence, sequence[1:], strict=False))):
                    raise CorruptState("waiting records have invalid durable FIFO sequences")
        if state["version"] == STATE_VERSION:
            try:
                QueueJournal(self).validate(state)
            except JournalError as exc:
                raise CorruptState(str(exc)) from exc
        return state

    def _publish(self, state: dict) -> None:
        from garuda.runtime.strict_store import StorageUnavailable, write_locked_document

        try:
            write_locked_document(self._active_directory_fd, self._state_path, state)
        except StorageUnavailable as exc:
            raise CorruptState(str(exc)) from exc

    @contextlib.contextmanager
    def _locked(self) -> Iterator[dict]:
        """Hold the exclusive lock, yield the state, write it back atomically."""
        from garuda.runtime.strict_store import StorageUnavailable, exclusive_lock

        try:
            with exclusive_lock(self.root) as directory_fd:
                self._active_directory_fd = directory_fd
                try:
                    state, migrated = self._read()
                    before = json.dumps(state, sort_keys=True)
                    if migrated:
                        queue_legacy.preserve_previous(directory_fd, state)
                        state["version"] = STATE_VERSION
                        state["pending"] = {}
                    yield state
                    if json.dumps(state, sort_keys=True) != before:
                        self._publish(state)
                finally:
                    del self._active_directory_fd
        except (queue_legacy.LegacyQueueError, JournalError) as exc:
            raise CorruptState(str(exc)) from exc
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
        if (not isinstance(scope, str) or not scope
                or any(value is not None and (not isinstance(value, str) or not value)
                       for value in (item_id, harness, user, session_id, config_digest))):
            raise QueueError("queue identifiers and explicit bindings must be nonempty strings")
        item_id = item_id if item_id is not None else uuid.uuid4().hex
        binding = {"user": user or current_user(), "harness": harness or _harness_of(scope),
                   "session_id": session_id, "config_digest": config_digest}
        try:
            check_scope_binding(scope, binding)
        except ValueError as exc:
            raise QueueError(str(exc)) from exc
        with self._locked() as state:
            if any(record["entry"]["id"] == item_id for record in state["pending"].values()):
                raise QueueError(f"queue item {item_id!r} has an unresolved transaction")
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

    def try_claim(self, scope: str, item_id: str, owner: Owner | None = None) -> bool:
        """Select work only after durable queue intent, reservation and commit."""
        owner = owner or current_owner()
        ticket = None
        with self._locked() as state:
            journal = QueueJournal(self)
            journal.recover(state)
            scope_state = self._scope(state, scope)
            journal.expire(state, scope, self._clock())
            waiting = scope_state["waiting"]
            if not waiting or waiting[0]["id"] != item_id:
                return False
            ticket = journal.claim(state, scope, dict(waiting[0]), owner)
        if ticket is None:
            return False
        adopt(self.capacity.root, ticket)
        self._claimed_owners[(scope, item_id)] = owner
        return True

    def recover_pending(self) -> list[dict]:
        """Reconcile proved safe dead-owner transactions without launching work."""
        with self._locked() as state:
            journal = QueueJournal(self)
            outcomes = journal.recover(state)
            for scope in state["scopes"]:
                journal.expire(state, scope, self._clock())
            return outcomes

    def begin_dispatch(self, scope: str, item_id: str, owner: Owner) -> None:
        """Persist activation intent before the background runner can launch."""
        with self._locked() as state:
            claim = state["scopes"].get(scope, {}).get("claims", {}).get(item_id)
            if (claim is None or claim.get("owner") != owner.to_dict()
                    or self._claimed_owners.get((scope, item_id)) != owner
                    or owner.pid != os.getpid() or owner.identity in ("", "unknown")
                    or claim["transaction"] in state["pending"]):
                raise QueueError("dispatch requires the complete committed claim owner")
            journal = QueueJournal(self)
            ticket = journal.ticket(scope, item_id, claim)
            if not ticket.activation_ready:
                raise QueueError("dispatch requires frozen user/session/configuration bindings")
            journal.slots.transition(ticket, "activated")

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

    def _requeue(self, scope: str, item_id: str, owner: Owner) -> None:
        with self._locked() as state:
            claim = state["scopes"].get(scope, {}).get("claims", {}).get(item_id)
            if claim is None or claim.get("owner") != owner.to_dict():
                return
            QueueJournal(self).release(state, scope, item_id, claim, requeue=True)
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
            QueueJournal(self).release(state, scope, item_id, claim)
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
        pending = {record["entry"]["id"]: record for record in state.get("pending", {}).values()}
        for name, entry in sorted(state["scopes"].items()):
            if scope is not None and name != scope:
                continue
            for position, waiting in enumerate(entry["waiting"], start=1):
                out.append({"scope": name, "state": "queued", "position": position,
                            **{k: waiting.get(k) for k in ENTRY_FIELDS},
                            **({"quarantined": True, "pending_operation": pending[waiting["id"]]["operation"]}
                               if waiting["id"] in pending else {})})
            for item_id, claim in entry["claims"].items():
                out.append({"scope": name, "state": "running", "position": 0, "id": item_id,
                            "user": claim.get("user"), "harness": claim.get("harness"),
                            "session_id": claim.get("session_id"),
                            "config_digest": claim.get("config_digest"),
                            "worker": claim.get("owner"), "heartbeat": claim.get("heartbeat"),
                            "quarantined": bool(claim.get("quarantined")) or item_id in pending,
                            **({"pending_operation": pending[item_id]["operation"]}
                               if item_id in pending else {})})
        listed = {row["id"] for row in out}
        for item_id, record in pending.items():
            if item_id not in listed and (scope is None or scope == record["scope"]):
                out.append({"scope": record["scope"], "state": "quarantined", "position": 0,
                            **{k: record["entry"].get(k) for k in ENTRY_FIELDS},
                            "quarantined": True, "pending_operation": record["operation"]})
        return out
