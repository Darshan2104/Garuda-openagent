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
owner is confirmed dead is reclaimed; a live owner, or one whose liveness
cannot be determined, keeps it.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from pathlib import Path

from garuda.runtime.ownership import Owner, current_owner, owner_liveness
from garuda.runtime.strict_store import StorageError, exclusive_lock, read_document, write_document


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
        return None
    if not isinstance(table, dict):
        raise CapacityError("global settings: `capacity` must map runtime ids to positive integers")
    value = table.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise CapacityError(f"global settings: capacity.{key} must be a positive integer, got {value!r}")
    return value


@dataclass(frozen=True)
class Reservation:
    key: str
    holder: str
    owner: Owner


class CapacityStore:
    """Per-key slot reservations under one owner-only directory."""

    def __init__(self, root: str | Path | None = None, *, liveness=owner_liveness):
        self.root = Path(root) if root else default_capacity_root()
        self._liveness = liveness

    def _path(self, key: str) -> Path:
        return self.root / f"{hashlib.sha256(key.encode()).hexdigest()[:32]}.json"

    def _load(self, key: str) -> dict:
        try:
            document = read_document(self._path(key), versions=(1,))
        except StorageError as exc:
            raise CapacityError(str(exc)) from exc
        if document is None:
            return {"version": 1, "key": key, "slots": {}}
        if not isinstance(document.get("slots"), dict):
            raise CapacityError(f"capacity record for {key!r} is malformed; refusing")
        return document

    def reserve(self, key: str, holder: str, ceiling: int, *, owner: Owner | None = None) -> Reservation:
        """Take a slot, retry the exact owner, or refuse live/unknown replacement."""
        if isinstance(ceiling, bool) or not isinstance(ceiling, int) or ceiling < 1:
            raise CapacityError(f"ceiling must be a positive integer, got {ceiling!r}")
        owner = owner or current_owner()
        try:
            with exclusive_lock(self.root):
                document = self._load(key)
                slots = document["slots"]
                existing = slots.get(holder)
                if existing is not None:
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
                    if other != holder and self._liveness(slot.get("owner") or {}) is False:
                        del slots[other]  # owner confirmed dead: reclaim
                if holder not in slots and len(slots) >= ceiling:
                    raise CapacityUnavailable(
                        f"runtime {key!r} is at its capacity of {ceiling} "
                        f"({', '.join(sorted(slots))} running)"
                    )
                slots[holder] = {"owner": owner.to_dict(), "reserved_at": time.time()}
                write_document(self._path(key), document)
        except StorageError as exc:
            raise CapacityError(f"capacity storage unavailable: {exc}") from exc
        return Reservation(key=key, holder=holder, owner=owner)

    def release(self, reservation: Reservation) -> None:
        """Give the slot back. A slot now held under another epoch is left alone."""
        try:
            with exclusive_lock(self.root):
                document = self._load(reservation.key)
                slot = document["slots"].get(reservation.holder)
                if slot is None or slot.get("owner") != reservation.owner.to_dict():
                    return
                del document["slots"][reservation.holder]
                write_document(self._path(reservation.key), document)
        except StorageError as exc:
            raise CapacityError(f"capacity storage unavailable: {exc}") from exc

    def holders(self, key: str) -> list[str]:
        """Inspect current holders. Never mutates."""
        return sorted(self._load(key)["slots"])
