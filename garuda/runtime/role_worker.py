"""Fixed owned role worker. Only the parent can authorize its lease operations."""

from __future__ import annotations

import asyncio
import json
import os
import time

from garuda.runtime.role_control import RoleControlClient, bounded_output
from garuda.runtime.role_request import catalog_from_document, native_from_document, role_from_document


async def execute(payload, control):
    """Run the role once. A role refusal (#738) is a typed failed result, not a crash.

    ``RoleRefused`` is how the role pipeline stops before or while applying the
    role (``role.start_failed``, ``role.model_unavailable``, ...). Raised out of
    this process it would only be a result-less exit, so its code would be lost.
    Any other exception still ends the worker without a result.
    """
    from garuda.runtime.roles import RoleRefused

    try:
        return await _execute(payload, control)
    except RoleRefused as exc:
        from garuda.context.redact import redact_text
        from garuda.core.sessions import SessionStore
        from garuda.runtime.role_control import MAX_REFUSAL_MESSAGE, refusal_code

        if not refusal_code(exc.code):
            raise  # not a role-pipeline code: a result-less exit, as before
        request = payload["request"]
        if "workspace_plan" in request:
            try:
                SessionStore(request["sessions_root"]).update_meta(request["session_id"], {
                    **request["workspace_plan"], "workspace_owner": request["workspace_owner"]})
            except (OSError, ValueError):
                pass  # the session may not have begun; the result still closes the step
        message = str(exc).removeprefix(f"{exc.code}: ")
        message = " ".join(redact_text(message)[0].splitlines())[:MAX_REFUSAL_MESSAGE]
        return {"session_id": request["session_id"], "success": False, "output": "",
                "refusal": {"code": exc.code, "message": message}}


async def _execute(payload, control):
    from garuda.core.sessions import SessionStore

    request = payload["request"]
    #: The step's liveness limits in seconds (#828); absent for a step without ``limits``.
    limits = request.get("limits") or {}
    role = role_from_document(request["role"])
    store = SessionStore(request["sessions_root"])
    session_id = request["session_id"]
    workspace_record = ({**request["workspace_plan"], "workspace_owner": request["workspace_owner"]}
                        if "workspace_plan" in request else {})
    if request["kind"] == "native":
        from garuda.agents.setup import activate_agent_plan
        from garuda.core.events import EventStore
        from garuda.interfaces.runner import run_agent_task

        prepared = await activate_agent_plan(native_from_document(request["native"]),
                                             approval_handler=control.approve, load_project_tools=False)
        deadline = limits.get("deadline_sec")
        if deadline:  # the step's own deadline (#828), within any the agent sets
            prepared.config.deadline_sec = min(prepared.config.deadline_sec or deadline, deadline)
        events = EventStore(session_id)
        started = time.monotonic()
        result = await run_agent_task(
            task=request["prompt"], model=prepared.reasoning, agent=prepared.agent,
            tools=prepared.tools, config=prepared.config, permissions=prepared.permissions,
            workspace=request["workspace"], events=events, mcp_manager=prepared.mcp_manager,
            lease_capability=control, store=store, external_approvals=request.get("external_approvals", False),
            session_record={**workspace_record, "flow_step": request["flow"],
                            **({"role": role.record()} if role else {})},
            collection_model=prepared.collection, collection_policy=prepared.collection_policy)
        timed_out = bool(deadline and not result.success and time.monotonic() - started >= deadline)
        return {"session_id": session_id, "success": bool(result.success),
                "output": bounded_output(result.final_message or ""),
                **({"failure": {"code": "run.deadline_exceeded", "liveness": "deadline"}}
                   if timed_out else {})}
    if request["kind"] == "acp":
        from garuda.interfaces.runtime_cli import run_acp_task
        from garuda.runtime.protocol import AgentRuntimeError, RuntimeStartError
        from garuda.workspace.no_edits import deny_all

        watchdog = {key: limits.get(key) for key in ("silence_sec", "tool_silence_sec")}
        hook = None
        if request.get("turns") is not None:
            from garuda.flows.long_step import StepTurns

            # A prepared confinement container is one harness process: its step
            # repairs and continues in that session but never rotates.
            hook = StepTurns(request["turns"], step_prompt=request["prompt"], store=store,
                             workspace=request["workspace"], session_id=session_id,
                             can_rotate=request.get("owned_container") is None)
        try:
            summary = await run_acp_task(
                request["prompt"], runtime_id=request["manifest"]["runtime_id"],
                workspace=request["workspace"], store=store,
                catalog=catalog_from_document(request["manifest"]), role_plan=role,
                lease_capability=control, session_id=session_id,
                owned_container=request.get("owned_container"),
                owned_docker=request.get("owned_docker"),
                admitted_runtime=(role.fallback or {}).get("admitted_runtime"),
                session_record={**workspace_record, "flow_step": request["flow"]},
                external_approvals=request.get("external_approvals", False),
                approval=deny_all if request["no_edits"] else control.approve,
                emit=lambda *_a: None, no_edits=request["no_edits"],
                **({"deadline_sec": limits["deadline_sec"]} if limits.get("deadline_sec") else {}),
                **({"silence_watchdog": watchdog} if any(watchdog.values()) else {}),
                **({"after_turn": hook} if hook is not None else {}))
        except RuntimeStartError:
            raise  # a refusal before launch keeps its own code
        except AgentRuntimeError as exc:
            # The agent stopped on its own (ended its turn, exited, timed out, or the
            # step's own deadline or silence limit stopped it): a failed step with a
            # complete result and its typed cause, as in-process launch reports it.
            if workspace_record:
                try:
                    store.update_meta(session_id, workspace_record)
                except (OSError, ValueError):
                    pass  # the session may not have begun; the result still closes the step
            return {"session_id": session_id, "success": False, "output": "",
                    "failure": failure_of(exc, limited=bool(limits)), **_turns(hook)}
        if workspace_record:
            store.update_meta(session_id, workspace_record)
        success = summary.get("status") == "completed"
        stop_reason = summary.get("stop_reason")
        output = str(summary.get("reply") or "")
        if hook is not None:
            output = hook.output
            if hook.stop is not None:
                success = False  # the step's own turn loop paused it; the code rides in `turns`
        return {"session_id": session_id, "success": success,
                "output": bounded_output(output),
                **({"failure": {"stop_reason": stop_reason[:64]}}
                   if not success and isinstance(stop_reason, str) else {}),
                **_turns(hook)}
    raise ValueError("unsupported role kind")


def _turns(hook) -> dict:
    """The result's ``turns`` record, when the step's turns did more than one plain turn."""
    record = hook.record() if hook is not None else None
    return {"turns": record} if record is not None else {}


def failure_of(exc: BaseException, *, limited: bool) -> dict:
    """Why the agent stopped (#828): its stable code, a redacted bounded detail, the
    harness's raw ``stopReason`` (#824) and which of the step's limits fired."""
    from garuda.context.redact import redact_text
    from garuda.core.failure_evidence import classify_exception
    from garuda.runtime.role_control import MAX_REFUSAL_MESSAGE

    code = getattr(exc, "code", None)
    if not isinstance(code, str) or not code:
        code = classify_exception(exc)[0]
    failure = {"code": code[:120],
               "detail": " ".join(redact_text(str(exc))[0].split())[:MAX_REFUSAL_MESSAGE]}
    stop_reason = getattr(exc, "acp_stop_reason", None)
    if isinstance(stop_reason, str):
        failure["stop_reason"] = stop_reason[:64]
    liveness = getattr(exc, "liveness", None)
    if limited and liveness in ("deadline", "silence"):
        failure["liveness"] = liveness
    return failure


def main():
    descriptor = int(os.environ.pop("GARUDA_OWNED_INPUT_FD"))
    with os.fdopen(descriptor, "rb") as handle:
        raw = handle.read(2_097_153)
    if len(raw) > 2_097_152:
        raise ValueError("role input exceeds its bound")
    payload = json.loads(raw)
    control = RoleControlClient(payload["control"])
    try:
        result = asyncio.run(execute(payload, control))
        control.request("result", result)
    finally:
        control.close()


if __name__ == "__main__":
    main()
