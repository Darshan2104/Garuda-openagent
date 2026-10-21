"""Detached owned starter worker: wait, validate, dispatch once, then prove closure."""

from __future__ import annotations

import asyncio

from garuda.interfaces.bg_sessions import BackgroundRefused
from garuda.scenarios.background import BackgroundAdmission, checked_request, matches


async def run(session_id, document, *, store, queue=None, poll_max=1.0):
    from dataclasses import asdict

    from garuda.agents.role_runs import RoleRunService
    from garuda.runtime import session_state
    from garuda.runtime.capacity import CapacityStore
    from garuda.runtime.ownership import current_owner
    from garuda.runtime.queue import QueueStore
    from garuda.scenarios.authority import start_authority
    from garuda.scenarios.compile import compile_with_context
    from garuda.scenarios.launch_intents import LaunchIntentStore
    from garuda.workspace.lease import LeaseStore

    request = checked_request(document)
    binding, saved = request["binding"], request["plan"]
    intent = LaunchIntentStore(store.root).read(binding["operation_id"])
    if (intent is None or intent["version"] != 2 or intent["phase"] != "launch-authorized"
            or intent["reserved_session"] != session_id or intent["queue_admission"] is None):
        raise BackgroundRefused("owned launch has no authorized queue receipt")
    receipt = intent["queue_admission"]
    operation = {"binding": binding, "queue": receipt}
    if (store.load_meta(session_id).get("background_operation") != operation
            or receipt["config_digest"] != document["config_digest"]
            or receipt["queue_root"] != request["queue_root"]
            or any(binding.get(key) != intent[key] for key in (
                "operation_id", "plan_digest", "authority_digest", "request_digest", "workspace_identity", "owner"))):
        raise BackgroundRefused("background request no longer agrees with its intent")
    queue = queue or QueueStore(request["queue_root"], capacity=CapacityStore(request["capacity_root"]))
    if (str(queue.root.resolve()) != request["queue_root"]
            or str(queue.capacity.root.resolve()) != request["capacity_root"]):
        raise BackgroundRefused("background queue storage changed")
    owner = current_owner()
    if owner.identity in {"", "unknown"} or owner.pid <= 1 or owner.pgid <= 1:
        raise BackgroundRefused("background worker identity is unavailable")

    def reserve(meta):
        previous = meta.get("worker")
        if previous is not None and any(previous.get(key) != owner.to_dict()[key]
                                        for key in ("pid", "identity", "pgid")):
            raise BackgroundRefused("another worker already owns this launch")
        if isinstance(previous, dict) and "epoch" in previous and previous != owner.to_dict():
            raise BackgroundRefused("this worker already dispatched the launch")
        if meta.get("background_cancelled_before_dispatch") is True:
            return {}
        return {"worker": owner.to_dict(), "state": session_state.queued(owner.to_dict())}

    meta = store.mutate_meta(session_id, reserve)
    if meta.get("background_cancelled_before_dispatch") is True:
        return 0
    scope, claimed, heartbeat = receipt["scope"], False, None
    code = 1
    try:
        delay = 0.05
        while True:
            rows = [row for row in queue.entries() if row["id"] == session_id]
            if not rows:
                return 0
            if len(rows) != 1 or not matches(rows[0], receipt) or rows[0].get("quarantined"):
                raise BackgroundRefused("queued launch binding changed")
            if store.load_meta(session_id).get("background_cancelled_before_dispatch") is True:
                return 0
            if queue.try_claim(scope, session_id, owner):
                claimed = True
                break
            await asyncio.sleep(delay)
            delay = min(delay * 2, poll_max)
        # Compile once in the admitted worker, before any dispatch. Configuration
        # changes while waiting refuse rather than selecting a different runtime.
        plan, context = await asyncio.to_thread(
            compile_with_context, saved["starter_id"], saved["inputs"], saved["workspace"], store=store)
        if (plan.to_dict() != saved or [asdict(source) for source in context.source_facts] != request["sources"]):
            raise BackgroundRefused("admitted starter sources changed while queued")
        authority = start_authority(
            plan, context, max_permission=request["permission"], server_epoch=binding["owner"]["epoch"],
            backend_available=True, approval_available=True)
        if not authority["can_start"] or authority["digest"] != binding["authority_digest"]:
            raise BackgroundRefused("background authority changed")
        queue.begin_dispatch(scope, session_id, owner)
        admission = BackgroundAdmission(queue, session_id, scope, owner, operation)
        store.update_meta(session_id, {"status": "running", "state": session_state.started(owner.to_dict())})
        from garuda.interfaces.bg_sessions import HEARTBEAT_EVERY, _heartbeat

        heartbeat = asyncio.create_task(_heartbeat(queue, scope, session_id, owner, HEARTBEAT_EVERY))

        async def admitted(*_args):
            return None  # intent records authorization; the worker publishes live progress

        async def deny(_action):
            return False  # children use their durable file channel, never this server callback

        work = asyncio.create_task(RoleRunService(store).run_prepared(
            plan, context, run_session=session_id, operation_binding=binding,
            on_admitted=admitted, approval_handler=deny, permission_mode=request["permission"],
            background_admission=admission))
        try:
            done, _pending = await asyncio.wait({work, heartbeat}, return_when=asyncio.FIRST_COMPLETED)
            if heartbeat in done:
                raise BackgroundRefused("background queue heartbeat stopped")
            result = work.result()
        except BaseException:
            work.cancel()
            await asyncio.gather(work, return_exceptions=True)
            raise
        code = 0 if result.flow.completed else 1
    except asyncio.CancelledError:
        code = 130
        if store.load_meta(session_id).get("flow_state") != "quarantined":
            store.update_meta(session_id, {"status": "cancelled", "flow_state": "interrupted",
                                           "state": session_state.interrupted(cancelled=True)})
    except Exception:
        import traceback

        traceback.print_exc()
        if store.load_meta(session_id).get("flow_state") != "quarantined":
            store.update_meta(session_id, {"status": "failed", "flow_state": "stopped",
                                           "state": session_state.interrupted()})
    finally:
        if heartbeat is not None:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
        # Missing evidence keeps capacity quarantined; dead worker cleanup is
        # never inferred from a terminal-looking session document.
        if claimed and not LeaseStore().live_holders_for_session(session_id):
            if store.load_meta(session_id).get("flow_state") != "quarantined":
                if queue.release(scope, session_id, owner):
                    store.update_meta(session_id, {"background_closure": {
                        "version": 1, "worker": owner.to_dict(),
                        "queue_released": True, "workspace_released": True}})
        elif not claimed and queue.cancel(scope, session_id):
            store.update_meta(session_id, {"background_cancelled_before_dispatch": True,
                                           "flow_state": "interrupted"})
    return code
