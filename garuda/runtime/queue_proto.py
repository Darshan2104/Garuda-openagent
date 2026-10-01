"""Prototype durable queue and capacity store — plan task A.4 (#153).

A spike, not product wiring: nothing in the CLI, SDK or web uses it yet. It exists
to prove, with real processes, the contract later tasks build on (B.0 strict
storage and shared capacity, D.1 production queue):

- **Mutual exclusion across processes.** Every mutation happens under an
  exclusive ``fcntl.flock`` on ``<root>/.lock``. If the lock cannot be taken
  (an unsupported filesystem, a symlinked lock file), the mutation refuses with
  :class:`LockUnavailable`; it never falls through to an unlocked write.
- **One finite capacity per scope.** A scope (for example ``user:harness``) has a
  ceiling. Claims from every kind of caller — foreground, background, SDK, flow
  or consult — count against the same ceiling.
- **FIFO within a scope.** Entries carry a monotonic sequence number; only the
  oldest waiting entry in a scope may claim.
- **Ownership needs proof of life to keep and proof of death to take.** An owner
  is identified by pid, process start identity (so a reused pid is not mistaken
  for the owner) and process group. A claim past its lease TTL is reclaimed only
  when the owner is *confirmed* dead; a live owner past its TTL keeps it, and an
  owner whose liveness cannot be determined is quarantined, never taken over.
- **Atomic, owner-only storage.** State is one JSON document written to a
  temporary file, fsynced, renamed over the old one, and the directory fsynced.
  Files are ``0600`` in a ``0700`` directory and are opened without following
  symlinks.
- **Wake-up by polling.** A waiter re-checks under the lock with bounded
  exponential backoff. Polling is portable and cannot lose a wake-up the way an
  edge-triggered notification can; the cost is a small, bounded latency.

POSIX only (macOS, Linux). Windows is refused at construction.
"""

from __future__ import annotations

import contextlib
import errno
import json
import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator

STATE_VERSION = 1


class QueueError(Exception):
    """Base error for the prototype store."""


class LockUnavailable(QueueError):
    """The cross-process lock could not be taken; nothing was changed."""


class CorruptState(QueueError):
    """The state document is unreadable or from an unknown version."""


@dataclass(frozen=True)
class Owner:
    """Who holds a claim: enough to tell the same process from a reused pid."""

    pid: int
    identity: str
    pgid: int

    def to_dict(self) -> dict:
        return {"pid": self.pid, "identity": self.identity, "pgid": self.pgid}


Liveness = Callable[[dict], "bool | None"]


def current_owner() -> Owner:
    from garuda.runtime.recovery import _process_identity

    pid = os.getpid()
    return Owner(pid=pid, identity=_process_identity(pid) or "unknown", pgid=os.getpgid(pid))


def owner_liveness(owner: dict) -> bool | None:
    """True = the recorded owner is alive, False = confirmed dead, None = unknown.

    A live pid whose start identity differs from the recorded one is a reused pid:
    the recorded owner is dead. A recorded identity of ``unknown`` can never be
    confirmed either way.
    """
    from garuda.runtime.recovery import _process_identity, _process_live

    pid = owner.get("pid")
    recorded = owner.get("identity")
    if not isinstance(pid, int) or not isinstance(recorded, str) or recorded == "unknown":
        return None
    live = _process_live(pid)
    if live is None:
        return None
    if live:
        current = _process_identity(pid)
        if current is None:
            return None
        if current != recorded:
            return False  # pid reused by another process: the owner is gone
        return True
    # The leader is gone; a descendant still in its process group keeps it alive.
    pgid = owner.get("pgid")
    if isinstance(pgid, int) and pgid > 1:
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return None
        return True
    return False


class QueueStore:
    """A cross-process FIFO queue with per-scope capacity, backed by one directory."""

    def __init__(
        self,
        root: str | Path,
        *,
        lease_ttl: float = 30.0,
        liveness: Liveness = owner_liveness,
    ):
        if os.name != "posix":
            raise QueueError("the prototype queue store supports POSIX hosts only")
        self.root = Path(root)
        self.lease_ttl = lease_ttl
        self._liveness = liveness
        if self.root.is_symlink():
            raise LockUnavailable(f"{self.root} is a symlink")
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)

    # -- storage ---------------------------------------------------------------

    @property
    def _state_path(self) -> Path:
        return self.root / "state.json"

    @contextlib.contextmanager
    def _locked(self) -> Iterator[dict]:
        """Hold the exclusive lock, yield the state, write it back atomically."""
        import fcntl

        try:
            fd = os.open(
                self.root / ".lock",
                os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
        except OSError as exc:
            raise LockUnavailable(f"cannot open the lock file: {exc}") from exc
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
            except OSError as exc:
                raise LockUnavailable(f"cannot lock {self.root}: {exc}") from exc
            state = self._read()
            before = json.dumps(state, sort_keys=True)
            yield state
            if json.dumps(state, sort_keys=True) != before:
                self._write(state)
        finally:
            os.close(fd)

    def _read(self) -> dict:
        try:
            fd = os.open(self._state_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except FileNotFoundError:
            return {"version": STATE_VERSION, "seq": 0, "scopes": {}}
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise CorruptState(f"{self._state_path} is a symlink") from exc
            raise
        with os.fdopen(fd, "r", encoding="utf-8") as handle:
            try:
                state = json.load(handle)
            except ValueError as exc:
                raise CorruptState(f"{self._state_path} is not valid JSON") from exc
        if not isinstance(state, dict) or state.get("version") != STATE_VERSION:
            raise CorruptState(f"{self._state_path} has an unsupported version")
        return state

    def _write(self, state: dict) -> None:
        tmp = self.root / f".state.{os.getpid()}.{uuid.uuid4().hex}.tmp"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(tmp, flags, 0o600)
        try:
            os.write(fd, json.dumps(state, sort_keys=True).encode())
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, self._state_path)
        dir_fd = os.open(self.root, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)

    @staticmethod
    def _scope(state: dict, scope: str, capacity: int | None = None) -> dict:
        scopes = state.setdefault("scopes", {})
        entry = scopes.setdefault(scope, {"capacity": 1, "waiting": [], "claims": {}})
        if capacity is not None:
            if not isinstance(capacity, int) or capacity < 1:
                raise QueueError("capacity must be a positive integer")
            entry["capacity"] = capacity
        return entry

    # -- queue -------------------------------------------------------------------

    def configure(self, scope: str, capacity: int) -> None:
        with self._locked() as state:
            self._scope(state, scope, capacity)

    def enqueue(self, scope: str, item_id: str | None = None) -> str:
        """Add a waiting entry; returns its id. Order is the order of this call."""
        item_id = item_id or uuid.uuid4().hex
        with self._locked() as state:
            state["seq"] = int(state.get("seq", 0)) + 1
            self._scope(state, scope)["waiting"].append({"id": item_id, "seq": state["seq"]})
        return item_id

    def cancel(self, scope: str, item_id: str) -> bool:
        """Remove a waiting entry. True if it was waiting."""
        with self._locked() as state:
            waiting = self._scope(state, scope)["waiting"]
            kept = [w for w in waiting if w["id"] != item_id]
            removed = len(kept) != len(waiting)
            self._scope(state, scope)["waiting"] = kept
        return removed

    def _expire(self, scope_state: dict, now: float) -> None:
        """Reclaim claims past their TTL whose owner is confirmed dead."""
        for claim_id, claim in list(scope_state["claims"].items()):
            if now - float(claim.get("heartbeat", 0)) < self.lease_ttl:
                continue
            alive = self._liveness(claim.get("owner") or {})
            if alive is False:
                del scope_state["claims"][claim_id]
            elif alive is None:
                claim["quarantined"] = True  # unknown liveness: never taken over

    def try_claim(self, scope: str, item_id: str, owner: Owner | None = None) -> bool:
        """Claim capacity for ``item_id`` if it is next in line and a slot is free."""
        owner = owner or current_owner()
        with self._locked() as state:
            scope_state = self._scope(state, scope)
            self._expire(scope_state, time.time())
            waiting = scope_state["waiting"]
            if not waiting or waiting[0]["id"] != item_id:
                return False
            if len(scope_state["claims"]) >= scope_state["capacity"]:
                return False
            waiting.pop(0)
            scope_state["claims"][item_id] = {
                "owner": owner.to_dict(),
                "epoch": uuid.uuid4().hex,
                "heartbeat": time.time(),
            }
            return True

    def claim(
        self,
        scope: str,
        item_id: str,
        *,
        timeout: float = 30.0,
        owner: Owner | None = None,
        initial_delay: float = 0.01,
        max_delay: float = 0.25,
    ) -> bool:
        """Wait (polling with bounded backoff) until the item claims, or time out."""
        deadline = time.monotonic() + timeout
        delay = initial_delay
        while True:
            if self.try_claim(scope, item_id, owner):
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(delay)
            delay = min(delay * 2, max_delay)

    def claim_with_workspace(
        self,
        scope: str,
        item_id: str,
        acquire_workspace: Callable[[], bool],
        *,
        owner: Owner | None = None,
    ) -> bool:
        """Claim capacity, then the workspace; never wait on one while holding the other.

        The workspace is acquired outside the store lock. If it is not available
        right now, the capacity claim is released and the entry goes back to the
        head of its queue, so no slot is held by a process that cannot work.
        """
        if not self.try_claim(scope, item_id, owner):
            return False
        try:
            acquired = acquire_workspace()
        except BaseException:
            self._requeue(scope, item_id)
            raise
        if acquired:
            return True
        self._requeue(scope, item_id)
        return False

    def _requeue(self, scope: str, item_id: str) -> None:
        with self._locked() as state:
            scope_state = self._scope(state, scope)
            claim = scope_state["claims"].pop(item_id, None)
            if claim is not None:
                scope_state["waiting"].insert(0, {"id": item_id, "seq": 0})

    def heartbeat(self, scope: str, item_id: str) -> bool:
        with self._locked() as state:
            claim = self._scope(state, scope)["claims"].get(item_id)
            if claim is None:
                return False
            claim["heartbeat"] = time.time()
            return True

    def release(self, scope: str, item_id: str) -> bool:
        with self._locked() as state:
            return self._scope(state, scope)["claims"].pop(item_id, None) is not None

    def snapshot(self) -> dict[str, Any]:
        """A read-only copy of the state, for inspection and tests."""
        with self._locked() as state:
            return json.loads(json.dumps(state))
