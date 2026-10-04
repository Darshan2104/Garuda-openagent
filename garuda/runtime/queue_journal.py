"""Queue/capacity intent, publication and release coordination.

Called under the queue lock; lock order is always queue, then capacity. A
pending slot cannot grant launch authority through ordinary capacity callers.
Owner death permits reconciliation only before activation. Activation ambiguity
remains quarantined, never replayed from a waiting record.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict

from garuda.runtime.capacity import CapacityUnavailable, unadopt
from garuda.runtime.ownership import Owner
from garuda.runtime.queue_slots import ACTIVATED_PHASES, QueueSlots, QueueTicket


class JournalError(ValueError):
    """A persisted journal cannot prove the proposed cross-store operation."""


class QueueJournal:
    def __init__(self, queue):
        self.queue = queue
        self.slots = QueueSlots(queue.capacity)
        self.root = str(queue.root.resolve())
        self.capacity_root = str(queue.capacity.root.resolve())

    def ticket(self, scope: str, item_id: str, claim: dict) -> QueueTicket:
        if any(claim.get(k) != claim["entry"].get(k)
               for k in ("user", "harness", "session_id", "config_digest")):
            raise JournalError("queue claim disagrees with its admitted binding")
        return QueueTicket.create(self.root, scope, {"id": item_id, **claim["entry"]},
                                  Owner(**claim["owner"]), claim["transaction"])

    def _record(self, operation: str, scope: str, entry: dict, ticket: QueueTicket) -> dict:
        return {"operation": operation, "scope": scope, "entry": dict(entry),
                "ticket": asdict(ticket), "capacity_root": self.capacity_root,
                "phase": "intent"}

    def decode(self, transaction: str, record: dict) -> QueueTicket:
        try:
            serialized = record["ticket"]
            owner = Owner(**serialized["owner"])
            expected = QueueTicket.create(self.root, record["scope"], record["entry"], owner, transaction)
            actual = QueueTicket(**{**serialized, "owner": owner})
            if (actual != expected or record["capacity_root"] != self.capacity_root
                    or record["operation"] not in {"claim", "release", "requeue"}
                    or record["phase"] not in {"intent", "published"}):
                raise JournalError("queue journal identity or phase mismatch; refusing")
            return actual
        except (TypeError, KeyError, ValueError) as exc:
            raise JournalError(f"queue journal is malformed: {exc}; refusing") from exc

    def claim(self, state: dict, scope: str, entry: dict, owner: Owner) -> QueueTicket | None:
        ticket = QueueTicket.create(self.root, scope, entry, owner, uuid.uuid4().hex)
        journal = state["pending"]
        journal[ticket.transaction] = self._record("claim", scope, entry, ticket)
        self.queue._publish(state)
        try:
            self.slots.reserve(ticket, self.queue.ceiling_for(ticket.key))
        except CapacityUnavailable:
            del journal[ticket.transaction]
            self.queue._publish(state)
            return None
        scope_state = state["scopes"][scope]
        scope_state["waiting"].pop(0)
        now = self.queue._clock()
        scope_state["claims"][entry["id"]] = {
            "owner": owner.to_dict(), "epoch": uuid.uuid4().hex,
            "heartbeat": now, "claimed_at": now,
            **{k: entry.get(k) for k in ("user", "harness", "session_id", "config_digest")},
            "reserved": True, "entry": dict(entry), "transaction": ticket.transaction,
        }
        journal[ticket.transaction]["phase"] = "published"
        self.queue._publish(state)
        self.slots.transition(ticket, "selected")
        del journal[ticket.transaction]
        self.queue._publish(state)
        return ticket

    def release(self, state: dict, scope: str, item_id: str, claim: dict, *, requeue: bool = False,
                preactivation_only: bool = False) -> None:
        ticket = self.ticket(scope, item_id, claim)
        unadopt(self.queue.capacity.root, ticket.key, ticket.holder)
        operation = "requeue" if requeue else "release"
        state["pending"][ticket.transaction] = self._record(operation, scope, claim["entry"], ticket)
        self.queue._publish(state)
        self.slots.fence_release(ticket, preactivation_only=preactivation_only)
        scope_state = state["scopes"][scope]
        del scope_state["claims"][item_id]
        if requeue:
            scope_state["waiting"].append(dict(claim["entry"]))
            scope_state["waiting"].sort(key=lambda entry: entry["seq"])
        state["pending"][ticket.transaction]["phase"] = "published"
        self.queue._publish(state)
        self.slots.release(ticket)
        del state["pending"][ticket.transaction]
        self.queue._publish(state)

    def recover(self, state: dict) -> list[dict]:
        """Finish safe dead-owner operations; never launch or replay activated work."""
        outcomes = []
        for transaction, record in list(state["pending"].items()):
            ticket = self.decode(transaction, record)
            verdict = self.queue._liveness(ticket.owner.to_dict())
            phase = self.slots.phase(ticket)
            if phase in ACTIVATED_PHASES or verdict is not False:
                outcomes.append({"id": ticket.holder, "state": "quarantined"})
                continue
            scope_state = state["scopes"][record["scope"]]
            claim = scope_state["claims"].get(ticket.holder)
            if phase is None and claim is not None:
                raise JournalError("published queue claim has no paired reservation; refusing recovery")
            self.slots.fence_release(ticket, preactivation_only=True)
            if claim is not None:
                if self.ticket(record["scope"], ticket.holder, claim) != ticket:
                    raise JournalError("pending recovery cannot alter a replacement claim")
                del scope_state["claims"][ticket.holder]
                if record["operation"] == "requeue":
                    scope_state["waiting"].append(dict(record["entry"]))
                    scope_state["waiting"].sort(key=lambda entry: entry["seq"])
            # Publish removal while the protected slot still counts. A crash
            # here leaves the same recovery record and no activation authority.
            record["phase"] = "published"
            self.queue._publish(state)
            self.slots.release(ticket)
            del state["pending"][transaction]
            self.queue._publish(state)
            outcomes.append({"id": ticket.holder, "state": "recovered"})
        return outcomes

    def expire(self, state: dict, scope: str, now: float) -> None:
        scope_state = state["scopes"][scope]
        for item_id, claim in list(scope_state["claims"].items()):
            if claim["transaction"] in state["pending"]:
                claim["quarantined"] = True
                continue
            verdict = self.queue._liveness(claim["owner"])
            phase = self.slots.phase(self.ticket(scope, item_id, claim))
            if phase not in {"selected", "activated"}:
                raise JournalError("queue claim has no paired reservation; refusing takeover")
            if verdict is False and phase != "activated":
                self.release(state, scope, item_id, claim, preactivation_only=True)
            elif verdict is False or verdict is None:
                age = now - float(claim["heartbeat"])
                if verdict is False or age < 0 or age >= self.queue.lease_ttl:
                    claim["quarantined"] = True

    def validate(self, state: dict) -> None:
        pending = state.get("pending")
        if not isinstance(pending, dict):
            raise JournalError("queue pending transactions are malformed")
        for transaction, record in pending.items():
            if not isinstance(transaction, str) or not isinstance(record, dict):
                raise JournalError("queue transaction is malformed")
            self.decode(transaction, record)
            if record["scope"] not in state["scopes"]:
                raise JournalError("queue transaction names an absent scope")
        for scope, entry in state["scopes"].items():
            for item_id, claim in entry["claims"].items():
                try:
                    ticket = self.ticket(scope, item_id, claim)
                    if ticket.holder != item_id or ticket.key != claim["harness"]:
                        raise JournalError("queue claim binding is malformed")
                    if (type(claim["entry"].get("seq")) is not int
                            or not 1 <= claim["entry"]["seq"] <= state["seq"]):
                        raise JournalError("queue claim has no valid durable sequence")
                    json.dumps(asdict(ticket))
                except (KeyError, TypeError, ValueError) as exc:
                    raise JournalError(f"queue claim is malformed: {exc}") from exc
