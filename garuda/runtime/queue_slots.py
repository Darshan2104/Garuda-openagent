"""Protected queue reservations in the shared capacity document.

Queue and capacity are different files. Ordinary capacity callers must retain
every queue-bound slot until the queue coordinator completes its journaled
release, even if the reserving process has died. Selection is not activation.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from garuda.runtime.ownership import Owner, owner_liveness
from garuda.runtime.strict_store import StorageError, exclusive_lock, write_locked_document

if TYPE_CHECKING:
    from garuda.runtime.capacity import CapacityStore

PHASES = {"pending", "selected", "activated", "releasing_selected", "releasing_activated"}
ACTIVATED_PHASES = {"activated", "releasing_activated"}


def check_scope_binding(scope: str, entry: dict) -> None:
    """Dispatch-ready work has one lane for its declared user and harness.

    Incomplete low-level allocations cannot activate. Do not turn them into
    dispatch-ready sessions by inferring missing admission fields.
    """
    ready = all(isinstance(entry.get(k), str) and entry[k]
                for k in ("user", "harness", "session_id", "config_digest"))
    if ready and scope != f'{entry["user"]}:{entry["harness"]}':
        raise ValueError("dispatch-ready binding disagrees with its user/harness scope")


@dataclass(frozen=True)
class QueueTicket:
    key: str
    holder: str
    owner: Owner
    transaction: str
    queue_root: str
    binding_digest: str
    activation_ready: bool

    @classmethod
    def create(cls, queue_root: str, scope: str, entry: dict, owner: Owner,
               transaction: str) -> QueueTicket:
        check_scope_binding(scope, entry)
        if (not all(isinstance(value, str) and value for value in (
                queue_root, scope, transaction, entry.get("id"), entry.get("harness"), entry.get("user")))
                or type(entry.get("seq")) is not int or entry["seq"] < 1):
            raise ValueError("queue ticket lacks complete identity or durable sequence")
        binding = {"scope": scope, **{k: entry.get(k) for k in (
            "id", "seq", "user", "harness", "session_id", "config_digest")}}
        if (type(owner.pid) is not int or owner.pid <= 0
                or type(owner.pgid) is not int or owner.pgid <= 0
                or not isinstance(owner.identity, str) or not owner.identity
                or not isinstance(owner.epoch, str) or not owner.epoch):
            raise ValueError("queue owner lacks a valid process identity and epoch")
        ready = all(isinstance(entry.get(k), str) and entry[k]
                    for k in ("user", "harness", "session_id", "config_digest"))
        digest = hashlib.sha256(json.dumps(binding, sort_keys=True).encode()).hexdigest()
        return cls(entry["harness"], entry["id"], owner, transaction, queue_root, digest, ready)

    def marker(self, phase: str) -> dict:
        return {"transaction": self.transaction, "queue_root": self.queue_root,
                "binding_digest": self.binding_digest, "phase": phase,
                "activation_ready": self.activation_ready}


def valid_marker(marker) -> bool:
    return (isinstance(marker, dict)
            and set(marker) == {"transaction", "queue_root", "binding_digest", "phase", "activation_ready"}
            and all(isinstance(marker[k], str) and marker[k]
                    for k in ("transaction", "queue_root", "binding_digest"))
            and isinstance(marker.get("phase"), str)
            and type(marker.get("activation_ready")) is bool
            and marker["phase"] in PHASES)


def can_activate(ticket: QueueTicket) -> bool:
    return (ticket.activation_ready and ticket.owner.pid == os.getpid()
            and ticket.owner.pgid == os.getpgid(0)
            and owner_liveness(ticket.owner.to_dict()) is True)


class QueueSlots:
    """The coordinator's side of a shared queue reservation, fenced by its ticket."""

    def __init__(self, capacity: CapacityStore):
        self.capacity = capacity

    @staticmethod
    def _same(slot: dict, ticket: QueueTicket) -> bool:
        marker = slot.get("queue")
        return (slot.get("owner") == ticket.owner.to_dict() and valid_marker(marker)
                and {k: marker[k] for k in ("transaction", "queue_root", "binding_digest", "activation_ready")}
                == {k: ticket.marker("pending")[k]
                    for k in ("transaction", "queue_root", "binding_digest", "activation_ready")})

    def reserve(self, ticket: QueueTicket, ceiling: int | None) -> None:
        from garuda.runtime.capacity import CapacityError, CapacityUnavailable

        if ceiling is not None and (type(ceiling) is not int or ceiling < 1):
            raise CapacityError("queue ceiling must be a positive integer or unlimited")
        try:
            with exclusive_lock(self.capacity.root) as directory_fd:
                document = self.capacity._load(ticket.key, for_mutation=True, directory_fd=directory_fd)
                slots = document["slots"]
                existing = slots.get(ticket.holder)
                if existing is not None:
                    if self._same(existing, ticket):
                        return
                    raise CapacityUnavailable("queue holder already has a different reservation")
                if ceiling is not None and len(slots) >= ceiling:
                    raise CapacityUnavailable(f"runtime {ticket.key!r} is at its capacity of {ceiling}")
                slots[ticket.holder] = {"owner": ticket.owner.to_dict(),
                                        "reserved_at": time.time(), "queue": ticket.marker("pending")}
                write_locked_document(directory_fd, self.capacity._path(ticket.key), document)
        except StorageError as exc:
            raise CapacityError(f"queue capacity storage unavailable: {exc}") from exc

    def transition(self, ticket: QueueTicket, phase: str) -> None:
        from garuda.runtime.capacity import CapacityError, CapacityUnavailable

        if phase not in {"selected", "activated"}:
            raise CapacityError("invalid queue reservation phase")
        if phase == "activated" and not can_activate(ticket):
            raise CapacityUnavailable("queue activation owner or frozen bindings cannot be proved")
        try:
            with exclusive_lock(self.capacity.root) as directory_fd:
                document = self.capacity._load(ticket.key, for_mutation=True, directory_fd=directory_fd)
                slot = document["slots"].get(ticket.holder)
                if slot is None or not self._same(slot, ticket):
                    raise CapacityUnavailable("queue reservation changed before commit")
                current = slot["queue"]["phase"]
                if current == phase:
                    write_locked_document(directory_fd, self.capacity._path(ticket.key), document)
                    return
                if (current, phase) not in {("pending", "selected"), ("selected", "activated")}:
                    raise CapacityUnavailable("queue reservation cannot skip or reverse a phase")
                slot["queue"]["phase"] = phase
                write_locked_document(directory_fd, self.capacity._path(ticket.key), document)
        except StorageError as exc:
            raise CapacityError(f"queue capacity storage unavailable: {exc}") from exc

    def phase(self, ticket: QueueTicket) -> str | None:
        """Read only; an absent slot is distinct from a replacement or corrupt one."""
        from garuda.runtime.capacity import CapacityUnavailable

        slot = self.capacity._load(ticket.key)["slots"].get(ticket.holder)
        if slot is None:
            return None
        if not self._same(slot, ticket):
            raise CapacityUnavailable("queue reservation does not match its journal")
        return slot["queue"]["phase"]

    def fence_release(self, ticket: QueueTicket, *, preactivation_only: bool = False) -> None:
        """Block captured adoption tickets before durable claim removal.

        Keep activation history in the fence: a dead owner cannot turn a
        previously activated release into safe pre-activation recovery.
        """
        from garuda.runtime.capacity import CapacityError, CapacityUnavailable

        try:
            with exclusive_lock(self.capacity.root) as directory_fd:
                document = self.capacity._load(ticket.key, for_mutation=True, directory_fd=directory_fd)
                slot = document["slots"].get(ticket.holder)
                if slot is None:
                    return
                if not self._same(slot, ticket):
                    raise CapacityUnavailable("queue release cannot alter a replacement slot")
                phase = slot["queue"]["phase"]
                if preactivation_only and phase in ACTIVATED_PHASES:
                    raise CapacityUnavailable("activated release cannot be recovered without cleanup proof")
                slot["queue"]["phase"] = ("releasing_activated" if phase in ACTIVATED_PHASES
                                           else "releasing_selected")
                write_locked_document(directory_fd, self.capacity._path(ticket.key), document)
        except StorageError as exc:
            raise CapacityError(f"queue capacity storage unavailable: {exc}") from exc

    def release(self, ticket: QueueTicket) -> None:
        """Called only after the coordinator durably removes its queue claim."""
        from garuda.runtime.capacity import CapacityError, CapacityUnavailable

        try:
            with exclusive_lock(self.capacity.root) as directory_fd:
                document = self.capacity._load(ticket.key, for_mutation=True, directory_fd=directory_fd)
                slot = document["slots"].get(ticket.holder)
                if slot is None:
                    return
                if not self._same(slot, ticket):
                    raise CapacityUnavailable("queue release cannot alter a replacement slot")
                if slot["queue"]["phase"] not in {"releasing_selected", "releasing_activated"}:
                    raise CapacityUnavailable("queue release requires a durable activation fence")
                del document["slots"][ticket.holder]
                write_locked_document(directory_fd, self.capacity._path(ticket.key), document)
        except StorageError as exc:
            raise CapacityError(f"queue capacity storage unavailable: {exc}") from exc
