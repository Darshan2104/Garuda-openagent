"""One finite capacity per runtime, shared by every launch (plan task B.0, #157).

Foreground runs, background workers, SDK and web sessions, flows and consults
all start runtimes through the same guard, so they draw from the same pool.
A capacity key names a runtime or provider (``native``, ``claude``, ``codex``)
— never a role alias or a selected model, so two names for the same harness
cannot double its capacity.

The ceilings are user settings, read only from the global settings file::

    # ~/.agent/settings.yaml
    capacity:
      claude: 2
      codex: 1

A key without a ceiling is not limited (today's behavior). Teams task C.1 will
supply packaged per-harness defaults.

A reservation that cannot be made now is refused immediately — no caller
waits on a slot (the background queue, D.1, adds waiting on top). A slot whose
ordinary owner is confirmed dead is reclaimed; a live or unknown owner keeps
it. Queue-bound slots remain protected until journaled coordinator release,
including after owner death. Nonempty version 1 records refuse mutation because
they do not record whether reservations came from a queue. Empty version 1
records upgrade only after a durable byte-exact private backup.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from garuda.runtime.ownership import Owner, current_owner, owner_liveness
from garuda.runtime.queue_slots import QueueTicket, can_activate, valid_marker
from garuda.runtime.strict_store import (
    StorageError,
    exclusive_lock,
    preserve_private_backup,
    read_document,
    read_private_bytes,
    write_locked_document,
)


class CapacityError(Exception):
    """Base error for capacity reservations."""


class CapacityUnavailable(CapacityError):
    """No slot is free for this runtime right now; nothing was reserved."""


def default_capacity_root() -> Path:
    from garuda.config.agent_home import global_settings_path

    return global_settings_path().expanduser().parent / "capacity"


def configured_ceiling(key: str, global_settings: dict | None = None) -> int | None:
    """The user's ceiling for ``key``, or ``None`` when it is not limited."""
    if global_settings is None:
        from garuda.config.agent_home import _load_global_settings

        global_settings = _load_global_settings()
    table = (global_settings or {}).get("capacity")
    if table is None:
        return _harness_ceiling(key)
    if not isinstance(table, dict):
        raise CapacityError("global settings: `capacity` must map runtime ids to positive integers")
    value = table.get(key)
    if value is None:
        return _harness_ceiling(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise CapacityError(f"global settings: capacity.{key} must be a positive integer, got {value!r}")
    return value


def _harness_ceiling(key: str) -> int | None:
    """``harnesses.<key>.max_parallel`` from the user's ``garuda.yaml`` (D.1).

    Only the user's file counts: a project cannot raise (or set) a ceiling. A
    file that cannot be read refuses rather than lift the limit."""
    from garuda.config import garuda_yaml

    try:
        document = garuda_yaml.load_file(garuda_yaml.user_path())
    except Exception as exc:
        raise CapacityError(f"garuda.yaml: cannot read the capacity ceilings: {exc}") from exc
    harness = ((document or {}).get("harnesses") or {}).get(key) or {}
    return harness.get("max_parallel")


@dataclass(frozen=True)
class Reservation:
    key: str
    holder: str
    owner: Owner


#: Successful committed claims retained by their claiming process, keyed by
#: capacity root, runtime and holder. A fork cannot inherit activation authority.
_ADOPTED: dict[tuple[str, str, str], QueueTicket] = {}
_ADOPTION_LOCK = threading.Lock()


def _after_fork() -> None:
    global _ADOPTION_LOCK
    _ADOPTION_LOCK = threading.Lock()
    _ADOPTED.clear()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_after_fork)


def adopt(root: Path, ticket: QueueTicket) -> None:
    key = (str(root.resolve()), ticket.key, ticket.holder)
    with _ADOPTION_LOCK:
        _ADOPTED[key] = ticket


def unadopt(root: Path, ticket: QueueTicket) -> None:
    key = (str(root.resolve()), ticket.key, ticket.holder)
    with _ADOPTION_LOCK:
        if _ADOPTED.get(key) == ticket:
            del _ADOPTED[key]


def is_adopted(root: Path, ticket: QueueTicket) -> bool:
    """Dispatch needs the full committed ticket, including after release failure."""
    key = (str(root.resolve()), ticket.key, ticket.holder)
    with _ADOPTION_LOCK:
        return _ADOPTED.get(key) == ticket


class CapacityStore:
    """Per-key slot reservations under one owner-only directory."""

    def __init__(self, root: str | Path | None = None, *, liveness=owner_liveness):
        self.root = Path(root) if root else default_capacity_root()
        self._liveness = liveness

    def _path(self, key: str) -> Path:
        return self.root / f"{hashlib.sha256(key.encode()).hexdigest()[:32]}.json"

    def _load(self, key: str, *, for_mutation: bool = False, directory_fd: int | None = None) -> dict:
        try:
            document = read_document(self._path(key), versions=(1, 2))
        except StorageError as exc:
            raise CapacityError(str(exc)) from exc
        if document is None:
            return {"version": 2, "key": key, "slots": {}}
        if document.get("key") != key or not isinstance(document.get("slots"), dict):
            raise CapacityError(f"capacity record for {key!r} is malformed; refusing")
        for holder, slot in document["slots"].items():
            if (not isinstance(holder, str) or not isinstance(slot, dict)
                    or not isinstance(slot.get("owner"), dict)
                    or ("queue" in slot and (document.get("version") != 2
                                             or not valid_marker(slot["queue"])))):
                raise CapacityError(f"capacity slot for {key!r} is malformed; refusing")
        if document.get("version", 1) == 1 and for_mutation:
            if document["slots"]:
                raise CapacityError("nonempty legacy capacity has no reservation origin evidence; "
                                    "resolve it with the prior version before upgrading")
            if directory_fd is None or set(document) != {"version", "key", "slots"}:
                raise CapacityError("legacy capacity upgrade has no locked or complete source")
            try:
                raw = read_private_bytes(directory_fd, self._path(key).name)
                if json.loads(raw) != document:
                    raise CapacityError("legacy capacity source changed before backup; refusing")
                preserve_private_backup(directory_fd, self._path(key).name + ".v1", raw)
            except (StorageError, ValueError) as exc:
                raise CapacityError(f"legacy capacity source/backup unavailable: {exc}") from exc
            document["version"] = 2
        return document

    def reserve(self, key: str, holder: str, ceiling: int, *, owner: Owner | None = None) -> Reservation:
        """Take a slot, retry the exact owner, or refuse live/unknown replacement."""
        if isinstance(ceiling, bool) or not isinstance(ceiling, int) or ceiling < 1:
            raise CapacityError(f"ceiling must be a positive integer, got {ceiling!r}")
        cache_key = (str(self.root.resolve()), key, holder)
        with _ADOPTION_LOCK:
            adopted = _ADOPTED.get(cache_key)
        if adopted is not None and adopted.owner.pid != os.getpid():
            adopted = None  # a fork cannot inherit a queued launch's authority
        owner = owner or (adopted.owner if adopted else None) or current_owner()
        try:
            with exclusive_lock(self.root) as directory_fd:
                document = self._load(key, for_mutation=True, directory_fd=directory_fd)
                slots = document["slots"]
                existing = slots.get(holder)
                if existing is None and adopted is not None:
                    raise CapacityUnavailable("queued launch no longer has its committed reservation")
                if existing is not None:
                    if "queue" in existing:
                        if (adopted is None or owner != adopted.owner
                                or not is_adopted(self.root, adopted)
                                or existing.get("owner") != owner.to_dict()
                                or (existing["queue"] != adopted.marker("selected")
                                    and existing["queue"] != adopted.marker("activated"))
                                or not can_activate(adopted)):
                            raise CapacityUnavailable("queue transaction has no committed launch authority")
                        existing["queue"]["phase"] = "activated"
                        # A prior publication may have failed its directory
                        # flush. Republish even an exact activation retry
                        # before returning authority to start a runtime.
                        write_locked_document(directory_fd, self._path(key), document)
                        if not is_adopted(self.root, adopted):
                            raise CapacityUnavailable("queue launch authority was revoked during activation")
                        return Reservation(key=key, holder=holder, owner=owner)
                    if existing.get("owner") == owner.to_dict():
                        return Reservation(key=key, holder=holder, owner=owner)
                    # A session id is a lookup key, not authority to steal its
                    # slot. This includes same-process callers with a new epoch.
                    if self._liveness(existing.get("owner") or {}) is not False:
                        raise CapacityUnavailable(
                            f"runtime {key!r}: holder {holder!r} already has a live "
                            "or unknown owner; refusing replacement"
                        )
                    del slots[holder]
                for other, slot in list(slots.items()):
                    if (other != holder and "queue" not in slot
                            and self._liveness(slot.get("owner") or {}) is False):
                        del slots[other]  # owner confirmed dead: reclaim
                if holder not in slots and len(slots) >= ceiling:
                    raise CapacityUnavailable(
                        f"runtime {key!r} is at its capacity of {ceiling} "
                        f"({', '.join(sorted(slots))} running)"
                    )
                slots[holder] = {"owner": owner.to_dict(), "reserved_at": time.time()}
                write_locked_document(directory_fd, self._path(key), document)
        except StorageError as exc:
            raise CapacityError(f"capacity storage unavailable: {exc}") from exc
        return Reservation(key=key, holder=holder, owner=owner)

    def release(self, reservation: Reservation) -> None:
        """Give the slot back. A slot now held under another epoch is left alone."""
        try:
            with exclusive_lock(self.root) as directory_fd:
                document = self._load(reservation.key, for_mutation=True, directory_fd=directory_fd)
                slot = document["slots"].get(reservation.holder)
                if slot is None or slot.get("owner") != reservation.owner.to_dict():
                    return
                if "queue" in slot:
                    return  # the queue coordinator releases after durable claim removal
                del document["slots"][reservation.holder]
                write_locked_document(directory_fd, self._path(reservation.key), document)
        except StorageError as exc:
            raise CapacityError(f"capacity storage unavailable: {exc}") from exc

    def holders(self, key: str) -> list[str]:
        """Inspect current holders. Never mutates."""
        return sorted(self._load(key)["slots"])
