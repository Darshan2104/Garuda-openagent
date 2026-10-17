"""Fixed owned role worker. Only the parent can authorize its lease operations."""

from __future__ import annotations

import asyncio
import json
import os

from garuda.runtime.role_control import RoleControlClient
from garuda.runtime.role_request import catalog_from_document, native_from_document, role_from_document


async def execute(payload, control):
    from garuda.core.sessions import SessionStore

    request = payload["request"]
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
        events = EventStore(session_id)
        result = await run_agent_task(
            task=request["prompt"], model=prepared.reasoning, agent=prepared.agent,
            tools=prepared.tools, config=prepared.config, permissions=prepared.permissions,
            workspace=request["workspace"], events=events, mcp_manager=prepared.mcp_manager,
            lease_capability=control, store=store,
            session_record={**workspace_record, "flow_step": request["flow"],
                            **({"role": role.record()} if role else {})},
            collection_model=prepared.collection, collection_policy=prepared.collection_policy)
        return {"session_id": session_id, "success": bool(result.success), "output": result.final_message or ""}
    if request["kind"] == "acp":
        from garuda.interfaces.runtime_cli import run_acp_task
        from garuda.workspace.no_edits import deny_all

        summary = await run_acp_task(
            request["prompt"], runtime_id=request["manifest"]["runtime_id"], workspace=request["workspace"],
            store=store, catalog=catalog_from_document(request["manifest"]), role_plan=role,
            lease_capability=control, session_id=session_id,
            owned_container=request.get("owned_container"),
            owned_docker=request.get("owned_docker"),
            admitted_runtime=(role.fallback or {}).get("admitted_runtime"),
            approval=deny_all if request["no_edits"] else control.approve, emit=lambda *_a: None)
        meta = store.load_meta(session_id)
        if workspace_record:
            store.update_meta(session_id, workspace_record)
        return {"session_id": session_id, "success": summary.get("status") == "completed",
                "output": str(meta.get("final_message") or "")}
    raise ValueError("unsupported role kind")


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
