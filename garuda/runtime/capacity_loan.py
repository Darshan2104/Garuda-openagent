"""Lend one issued capacity reservation without transferring its release authority."""

import uuid

from garuda.runtime.capacity import CapacityError, CapacityStore
from garuda.runtime.strict_store import exclusive_lock, write_locked_document


class CapacityLoan:
    """One live borrower of an admission owner's slot; no bare-id authority."""

    def __init__(self, store, reservation, *, quarantine):
        if type(store) is not CapacityStore:
            raise CapacityError("capacity loans require the issuing capacity store")
        self._store, self._reservation = store, reservation
        self._quarantine = quarantine
        self._epoch = uuid.uuid4().hex
        self._active = self._used = self._quarantined = False
        with exclusive_lock(store.root):
            document = store._load(reservation.key)
            store._check_issued(reservation, document["slots"].get(reservation.holder))
            if (reservation.key, reservation.holder) in store._loans:
                raise CapacityError("capacity reservation already has a loan")
            store._loans[reservation.key, reservation.holder] = self

    def _checked(self, document):
        reservation, store = self._reservation, self._store
        slot = document["slots"].get(reservation.holder)
        store._check_issued(reservation, slot)
        if store._loans.get((reservation.key, reservation.holder)) is not self:
            raise CapacityError("capacity loan has no issuing-store authority")
        return slot

    def acquire(self, key, holder):
        reservation, store = self._reservation, self._store
        if (key != reservation.key or holder != reservation.holder
                or self._used or self._quarantined):
            raise CapacityError("capacity loan is foreign, used or quarantined")
        with exclusive_lock(store.root) as directory_fd:
            document = store._load(key, for_mutation=True, directory_fd=directory_fd)
            slot = self._checked(document)
            if "loan" in slot:
                raise CapacityError("capacity loan already has a borrower")
            slot["loan"] = self._epoch
            try:
                write_locked_document(directory_fd, store._path(key), document)
            except Exception:
                self.quarantine()
                raise
        self._active = self._used = True

    def release(self):
        if not self._active or self._quarantined:
            return
        reservation, store = self._reservation, self._store
        try:
            with exclusive_lock(store.root) as directory_fd:
                document = store._load(reservation.key, for_mutation=True,
                                       directory_fd=directory_fd)
                slot = self._checked(document)
                if slot.get("loan") != self._epoch:
                    raise CapacityError("capacity borrower epoch changed; retaining ownership")
                # Keep the durable marker until the admission owner publishes
                # its receipt and releases the slot. A different store cannot
                # interpret an ended borrow as authority to drop ownership.
        except Exception:
            self.quarantine()
            raise
        self._active = False  # parent still owns the slot until durable completion

    def owner_may_release(self, slot):
        self._store._check_issued(self._reservation, slot)
        return (self._used and not self._active and not self._quarantined
                and slot.get("loan") == self._epoch
                and self._store._loans.get((self._reservation.key,
                                           self._reservation.holder)) is self)

    def quarantine(self):
        self._quarantined = True
        self._quarantine()
