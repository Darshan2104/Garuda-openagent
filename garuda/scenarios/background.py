"""Durable launch binding and read-only projection for owned background starters."""

from __future__ import annotations

import os
from dataclasses import asdict

from garuda.scenarios.digests import digest


def capsule(plan, context, binding, permission, queue):
    return {"version": 1, "plan": plan.to_dict(),
            "sources": [asdict(source) for source in context.source_facts],
            "binding": binding, "permission": permission,
            "queue_root": str(queue.root.resolve()),
            "capacity_root": str(queue.capacity.root.resolve())}


def checked_request(document):
    from garuda.interfaces.bg_sessions import BackgroundRefused

    if (type(document) is not dict
            or set(document) != {"version", "kind", "request", "config_digest"}
            or document.get("version") != 2 or document.get("kind") != "owned-starter"):
        raise BackgroundRefused("unsupported owned background launch")
    request = document["request"]
    if (type(request) is not dict or set(request) != {
            "version", "plan", "sources", "binding", "permission", "queue_root", "capacity_root"}
            or request.get("version") != 1 or digest(request) != document["config_digest"]
            or type(request.get("plan")) is not dict or request["plan"].get("kind") != "run"
            or request["plan"].get("options", {}).get("bg") is not True
            or type(request.get("sources")) is not list):
        raise BackgroundRefused("owned background launch binding disagrees")
    return request


def launch_owned(*, store, queue, spawn, reserved_session, operation_binding, request):
    from garuda.core.session_publication import protect_session
    from garuda.interfaces import bg_sessions
    from garuda.runtime import session_state
    from garuda.runtime.queue import scope_for
    from garuda.scenarios.launch_intents import LaunchIntentStore

    document = {"version": 2, "kind": "owned-starter", "request": request,
                "config_digest": digest(request)}
    checked_request(document)
    from garuda.runtime.role_request import encode_document

    if len(encode_document(document).encode("utf-8")) > 2_097_152:
        raise bg_sessions.BackgroundRefused("owned background request exceeds its bound")
    plan = request["plan"]
    intents = LaunchIntentStore(store.root)
    intent = intents.read(operation_binding["operation_id"])
    if (intent is None or intent["version"] != 2 or intent["phase"] != "launch-authorized"
            or intent["reserved_session"] != reserved_session
            or request["binding"] != operation_binding
            or any(operation_binding.get(key) != intent[key] for key in (
                "owner", "plan_digest", "authority_digest", "request_digest", "workspace_identity"))
            or plan["digest"] != intent["plan_digest"] or plan["workspace"] != intent["workspace"]
            or request["queue_root"] != str(queue.root.resolve())
            or request["capacity_root"] != str(queue.capacity.root.resolve())):
        raise bg_sessions.BackgroundRefused("owned background intent disagrees")
    from garuda.runtime.ownership import Owner

    harness = plan["bindings"][plan["target"]]["runtime_id"]
    receipt = {"queue_root": request["queue_root"], "scope": scope_for(harness),
               "item_id": reserved_session, "harness": harness,
               "config_digest": document["config_digest"]}
    intents.bind_queue(intent["operation_id"], owner=Owner(**intent["owner"]), admission=receipt)
    directory = store.session_dir(reserved_session)
    directory.mkdir(mode=0o700, exist_ok=False)  # even an identical retry must never spawn twice
    protect_session(directory / "meta.json", kind="flow-verification" if plan["checks"] else "owned-flow")
    store.begin(reserved_session, task=plan["task"], model="flow", agent=f"flow:run:{plan['target']}",
                workspace=plan["workspace"], name=plan["options"].get("name"))
    from garuda.scenarios.types import LaunchPlan

    store.update_meta(reserved_session, {
        "background": True, "status": "queued", "state": session_state.queued(),
        "kind": "flow", "launch_kind": "run", "flow_state": "queued",
        "starter": LaunchPlan(**plan).launch_metadata(), "config_digest": document["config_digest"],
        "background_operation": {"binding": operation_binding, "queue": receipt}, "worker": None})
    bg_sessions._write_private(directory / bg_sessions.LAUNCH_FILE, document)
    queue.enqueue(receipt["scope"], reserved_session, harness=harness, session_id=reserved_session,
                  config_digest=document["config_digest"])
    try:
        process = (spawn(reserved_session) if spawn is not None else
                   bg_sessions._spawn_worker(reserved_session, store_root=store.root))
    except BaseException:
        if queue.cancel(receipt["scope"], reserved_session):
            store.update_meta(reserved_session, {
                "status": "failed", "flow_state": "interrupted",
                "state": session_state.interrupted(), "background_cancelled_before_dispatch": True})
        raise
    record = bg_sessions.worker_record(process)

    def publish(meta):
        previous = meta.get("worker")
        if previous is not None:
            if any(previous.get(key) != record[key] for key in ("pid", "identity", "pgid")):
                raise bg_sessions.BackgroundRefused("worker publication conflicts with its owner")
            return {}  # worker may already have supplied its complete owner epoch
        return {"worker": record}

    store.mutate_meta(reserved_session, publish)
    return reserved_session


class BackgroundAdmission:
    """Live worker authority; the serialized request never substitutes for this ticket."""

    def __init__(self, queue, session_id, scope, owner, operation):
        from garuda.runtime.queue_journal import QueueJournal

        self.queue, self.session_id, self.scope, self.owner = queue, session_id, scope, owner
        self.operation = operation
        self.harness = operation["queue"]["harness"]
        with queue._locked() as state:
            claim = state["scopes"][scope]["claims"][session_id]
            self.ticket = QueueJournal(queue).ticket(scope, session_id, claim)
        self.validate()

    def validate(self):
        from garuda.interfaces.bg_sessions import BackgroundRefused
        from garuda.runtime.capacity import is_adopted
        from garuda.runtime.ownership import owner_liveness

        if (self.owner.pid != os.getpid() or self.owner.pgid != os.getpgid(0)
                or owner_liveness(self.owner.to_dict()) is not True
                or self.queue._claimed_owners.get((self.scope, self.session_id)) != self.owner
                or not is_adopted(self.queue.capacity.root, self.ticket)):
            raise BackgroundRefused("background owner has no live adopted queue ticket")
        row = next((row for row in self.queue.entries(self.scope) if row["id"] == self.session_id), None)
        if (row is None or row["state"] != "running" or row["worker"] != self.owner.to_dict()
                or row.get("quarantined") or not matches(row, self.operation["queue"])):
            raise BackgroundRefused("background claim no longer agrees with dispatch")


def matches(row, receipt):
    return (row.get("scope") == receipt["scope"] and row.get("id") == receipt["item_id"]
            and row.get("session_id") == receipt["item_id"]
            and row.get("harness") == receipt["harness"]
            and row.get("config_digest") == receipt["config_digest"])


def project(store, record, *, duplicate=False):
    """Inspect evidence only. Neither server death nor a missing row permits replay."""
    from garuda.interfaces.bg_sessions import _load_launch
    from garuda.runtime.capacity import CapacityStore
    from garuda.runtime.ownership import owner_liveness
    from garuda.runtime.queue import QueueStore
    from garuda.workspace.lease import LeaseStore

    payload = {"operation_id": record["operation_id"], "phase": record["phase"],
               "session_id": record["reserved_session"], "status": "uncertain",
               "outcome": None, "reason": "publication.uncertain", "background": True,
               "duplicate": duplicate}
    receipt = record.get("queue_admission")
    if receipt is None:
        return payload
    try:
        meta = store.load_meta(record["reserved_session"])
        request = checked_request(_load_launch(store, record["reserved_session"]))
        binding = request["binding"]
        if (meta.get("background_operation") != {"binding": binding, "queue": receipt}
                or meta.get("session_id") != record["reserved_session"]
                or meta.get("config_digest") != receipt["config_digest"]
                or digest(request) != receipt["config_digest"]
                or meta.get("publication_policy", {}).get("kind") not in {"owned-flow", "flow-verification"}
                or meta.get("publication_policy", {}).get("session_id") != record["reserved_session"]
                or meta.get("starter", {}).get("plan_digest") != record["plan_digest"]
                or request["plan"]["workspace"] != record["workspace"]
                or request["plan"]["digest"] != record["plan_digest"]
                or any(binding.get(key) != record[key] for key in (
                    "operation_id", "plan_digest", "authority_digest", "request_digest", "workspace_identity", "owner"))):
            return payload
        payload["flow_state"] = meta.get("flow_state")
        queue = QueueStore(receipt["queue_root"], capacity=CapacityStore(request["capacity_root"]))
        rows = [row for row in queue.entries() if row["id"] == record["reserved_session"]]
        if rows and (len(rows) != 1 or not matches(rows[0], receipt)):
            return payload
        final_binding = meta.get("flow_operation")
        if final_binding is not None:
            if (type(final_binding) is not dict or set(final_binding) != set(binding) | {"session_id", "lease_epoch"}
                    or any(final_binding.get(key) != value for key, value in binding.items())
                    or final_binding.get("session_id") != record["reserved_session"]
                    or not isinstance(final_binding.get("lease_epoch"), str)):
                return payload
            execution = meta.get("flow_execution_workspace")
            if execution is None:
                if meta.get("workspace") != record["workspace"]:
                    return payload
            elif (type(execution) is not dict or execution.get("source") != record["workspace"]
                  or execution.get("path") != meta.get("workspace") or meta.get("worktree") != meta.get("workspace")
                  or meta.get("source_repo") != record["workspace"] or meta.get("isolation") != "worktree"):
                return payload
        elif meta.get("flow_state") == "completed":
            return payload
        worker = meta.get("worker")
        worker_live = owner_liveness(worker) if isinstance(worker, dict) else None
        if not rows:
            if meta.get("background_cancelled_before_dispatch") is True:
                payload.update(status="terminal", outcome="interrupted", reason=None)
                return payload
            closure = meta.get("background_closure")
            if (type(closure) is dict and closure == {
                    "version": 1, "worker": meta.get("worker"),
                    "queue_released": True, "workspace_released": True}
                    and record["reserved_session"] not in queue.capacity.holders(receipt["harness"])
                    and not LeaseStore().live_holders_for_session(record["reserved_session"])
                    and meta.get("flow_state") in {"completed", "stopped", "interrupted"}):
                payload.update(status="terminal", outcome=meta["flow_state"], reason=None)
            elif worker_live is True:
                payload.update(status="admitted", reason="cleanup.pending")
            return payload
        row = rows[0]
        if row.get("pending_operation") == "release" and worker_live is True:
            payload.update(status="admitted", reason="cleanup.pending")
        elif row.get("quarantined") or meta.get("flow_state") == "quarantined":
            payload.update(status="quarantined", reason="background.quarantined")
        elif row["state"] == "queued":
            if isinstance(worker, dict) and owner_liveness(worker) is False:
                payload.update(status="uncertain", reason="worker.dead")
            else:
                payload.update(status="queued", reason=None, position=row["position"])
        elif row["state"] == "running" and row.get("worker") == worker:
            live = owner_liveness(worker)
            if live is True:
                payload.update(status="admitted", reason=None, flow_state=meta.get("flow_state"))
            elif live is False:
                payload.update(status="quarantined", reason="worker.dead")
    except Exception:
        pass  # malformed, missing and future evidence is uncertainty, never permission to dispatch
    return payload
