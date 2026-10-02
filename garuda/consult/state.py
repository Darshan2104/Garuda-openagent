"""Durable consult admission state per root task (plan task G.2, #170).

One small locked document per root task under ``<sessions>/.consult/<root>/``:

* ``count`` — consults launched for the root task and everything beneath it. It survives
  restarts and resume, and counts launches that failed; it is only refunded when dispatch
  provably never happened;
* ``active`` — the single in-flight request (one per root): a concurrent different request
  is ``consult.busy``, never queued;
* ``requests`` — each request id with the digest of its payload, its child id, its state
  and, once finished, its (text-free) result.

Reserving the counter and the request is one locked step, so concurrent callers can neither
exceed the count nor both become active. A repeated request id returns the same result (or
``pending`` while it runs); the same id with a different payload refuses.

**At-most-once dispatch.** The intent is persisted (``admitted``) before anything launches and
``dispatched`` just before the child starts. If the process dies with a request unresolved, the
next call *reconciles* it — never resends: an ``admitted`` request (nothing was sent) is
refunded; a ``dispatched`` one becomes ``interrupted``, and its caller is told so.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

from garuda.consult.errors import ConsultRefused
from garuda.runtime import strict_store as ss
from garuda.runtime.ownership import current_owner, owner_liveness

VERSION = 1
ADMITTED, DISPATCHED, DONE, INTERRUPTED, QUARANTINED = (
    "admitted", "dispatched", "done", "interrupted", "quarantined")


@dataclass
class Admission:
    kind: str  # new | replay | pending
    request: dict


class ConsultState:
    def __init__(self, store, root_session: str, *, liveness=owner_liveness, clock=time.time):
        self.directory = Path(store.root) / ".consult" / root_session
        self._path = self.directory / "state.json"
        self._liveness = liveness
        self._clock = clock

    # --- storage ---------------------------------------------------------------------

    def _read(self) -> dict:
        return ss.read_document(self._path, versions=(VERSION,)) or {
            "version": VERSION, "count": 0, "active": None, "requests": {}}

    def _locked(self):
        return ss.exclusive_lock(self.directory)

    def snapshot(self) -> dict:
        return self._read()

    # --- reconciliation ---------------------------------------------------------------

    def _reconcile(self, doc: dict) -> None:
        active = doc.get("active")
        if not active:
            return
        request = doc["requests"].get(active)
        if request is None or request["state"] in (DONE, INTERRUPTED):
            doc["active"] = None
            return
        if request["state"] == QUARANTINED:
            return  # a child that may still run keeps the root busy until an operator clears it
        if self._liveness(request.get("owner") or {}) is not False:
            return  # its owner is alive, or cannot be told apart from alive
        if request["state"] == ADMITTED:
            doc["count"] = max(0, doc["count"] - 1)  # dispatch provably never occurred
            del doc["requests"][active]
        else:
            request["state"] = INTERRUPTED
            request["result"] = {"outcome": "interrupted", "code": "consult.interrupted"}
        doc["active"] = None

    # --- admission --------------------------------------------------------------------

    def admit(self, request_id: str, digest: str, *, child_id: str, limit: int, asker: str,
              target: str) -> Admission:
        try:
            with self._locked():
                doc = self._read()
                self._reconcile(doc)
                known = doc["requests"].get(request_id)
                if known is not None:
                    if known["digest"] != digest:
                        raise ConsultRefused("consult.payload_changed",
                                             "that request id was used with a different payload")
                    kind = "replay" if known["state"] in (DONE, INTERRUPTED) else "pending"
                    ss.write_document(self._path, doc)
                    return Admission(kind, dict(known))
                if doc["active"]:
                    ss.write_document(self._path, doc)
                    raise ConsultRefused("consult.busy", "another consult is already in progress "
                                                         "for this task")
                if doc["count"] >= limit:
                    ss.write_document(self._path, doc)
                    raise ConsultRefused("consult.limit", f"this task has used its {limit} "
                                                          "consults")
                entry = {"digest": digest, "child_id": child_id, "state": ADMITTED, "asker": asker,
                         "target": target, "owner": current_owner().to_dict(),
                         "admitted_at": self._clock(), "result": None}
                doc["requests"][request_id] = entry
                doc["active"] = request_id
                doc["count"] += 1
                ss.write_document(self._path, doc)
                return Admission("new", dict(entry))
        except ss.StorageError as exc:
            raise ConsultRefused("consult.failed", f"admission storage unavailable: {exc}") from exc

    def _update(self, request_id: str, mutate) -> dict:
        with self._locked():
            doc = self._read()
            mutate(doc, doc["requests"][request_id])
            ss.write_document(self._path, doc)
            return dict(doc["requests"][request_id])

    def mark_dispatched(self, request_id: str) -> None:
        self._update(request_id, lambda doc, r: r.update(state=DISPATCHED,
                                                         dispatched_at=self._clock()))

    def refund(self, request_id: str) -> None:
        """Give the reservation back. Only valid before dispatch."""
        def undo(doc, request):
            if request["state"] != ADMITTED:
                raise ConsultRefused("consult.failed", "a dispatched consult cannot be refunded")
            doc["count"] = max(0, doc["count"] - 1)
            del doc["requests"][request_id]
            doc["active"] = None

        self._update_or_none(request_id, undo)

    def _update_or_none(self, request_id: str, mutate) -> None:
        with self._locked():
            doc = self._read()
            if request_id in doc["requests"]:
                mutate(doc, doc["requests"][request_id])
                ss.write_document(self._path, doc)

    def finish(self, request_id: str, result: dict) -> None:
        """Persist the terminal result and free the root for the next consult."""
        def done(doc, request):
            request["state"] = DONE
            request["result"] = result
            request["finished_at"] = self._clock()
            if doc["active"] == request_id:
                doc["active"] = None

        self._update(request_id, done)

    def quarantine(self, request_id: str, result: dict) -> None:
        """A child that may still be running: keep the root busy and the slot held."""
        def hold(doc, request):
            request["state"] = QUARANTINED
            request["result"] = result

        self._update(request_id, hold)
