"""Lend a claimed queue slot to one issued owned-role child."""

from __future__ import annotations

import os
import uuid

from garuda.runtime.capacity import CapacityError, is_adopted
from garuda.runtime.ownership import owner_liveness
from garuda.runtime.queue_journal import QueueJournal
from garuda.runtime.queue_slots import QueueSlots
from garuda.runtime.strict_store import exclusive_lock, write_locked_document


class QueueCapacityLoan:
    def __init__(self, admission, capability):
        from garuda.interfaces.run_guard import LeaseCapability

        if type(capability) is not LeaseCapability:
            raise CapacityError("queued role capacity requires an issued workspace capability")
        parent = capability._parent
        admission.validate()
        if (parent._queued_admission is not admission or parent.session_id != admission.session_id
                or parent._capacity_store is not admission.queue.capacity or not parent._held
                or capability.revoked or capability._parent_epoch != parent._epoch):
            raise CapacityError("queued role capacity belongs to another parent")
        self.admission, self.capability, self.parent = admission, capability, parent
        self.store, self.reservation = admission.queue.capacity, parent._reservation
        if self.reservation is None or self.reservation.holder != parent.session_id:
            raise CapacityError("queued parent has no issued reservation")
        with admission.queue._locked() as state:
            claim = state["scopes"][admission.scope]["claims"][admission.session_id]
            self.ticket = QueueJournal(admission.queue).ticket(admission.scope, admission.session_id, claim)
        self.epoch = uuid.uuid4().hex
        self.active = self.used = self.quarantined = False
        with exclusive_lock(self.store.root):
            key = (self.reservation.key, self.reservation.holder)
            if key in self.store._loans:
                raise CapacityError("queued reservation already has a borrower")
            self.store._loans[key] = self

    def _checked(self, slot):
        reservation, ticket, admission = self.reservation, self.ticket, self.admission
        if (ticket.owner.pid != os.getpid() or ticket.owner.pgid != os.getpgid(0)
                or owner_liveness(ticket.owner.to_dict()) is not True
                or admission.queue._claimed_owners.get((admission.scope, admission.session_id)) != ticket.owner
                or self.store._issued.get((reservation.key, reservation.holder)) is not reservation
                or self.store._loans.get((reservation.key, reservation.holder)) is not self
                or not isinstance(slot, dict) or not QueueSlots._same(slot, ticket)):
            raise CapacityError("queued capacity lacks its retained claiming owner")

    def acquire(self, key, holder):
        if (key != self.ticket.key or holder != self.capability.session_id or self.used
                or self.quarantined or self.capability.revoked
                or not is_adopted(self.store.root, self.ticket)):
            raise CapacityError("queued capacity loan is foreign, used or revoked")
        self.parent._check_authority()
        with exclusive_lock(self.store.root) as directory:
            document = self.store._load(key, for_mutation=True, directory_fd=directory)
            slot = document["slots"].get(self.ticket.holder)
            self._checked(slot)
            if "loan" in slot or slot["queue"]["phase"] != "activated":
                raise CapacityError("queued capacity is not exclusively activated")
            slot["loan"] = self.epoch
            try:
                write_locked_document(directory, self.store._path(key), document)
            except Exception:
                self.quarantine()
                raise
        self.active = self.used = True

    def release(self):
        if not self.active or self.quarantined:
            return
        with exclusive_lock(self.store.root):
            slot = self.store._load(self.ticket.key)["slots"].get(self.ticket.holder)
            self._checked(slot)
            if slot.get("loan") != self.epoch:
                self.quarantine()
                raise CapacityError("queued borrower epoch changed")
        self.active = False  # coordinator retains the slot through parent publication

    def owner_may_release(self, slot, ticket):
        self._checked(slot)
        return (ticket == self.ticket and self.used and not self.active and not self.quarantined
                and not self.parent._quarantined and slot.get("loan") == self.epoch)

    def quarantine(self):
        self.quarantined = True
        self.parent._quarantined = True
