"""Start one flow step as its own session (plan task C.6a, #158).

A native step runs through ``run_agent_task`` with the role's profile, exact
model, effort and permissions; an ACP step through ``run_acp_task`` with the
role's proven options. Both borrow the flow's lease through the step's
capability, so neither takes or keeps the workspace itself. A ``no-edits``
step runs native ``readonly`` or with every ACP approval denied.
"""

from __future__ import annotations

from garuda.flows.engine import StepLaunch, StepResult


async def launch_step(launch: StepLaunch) -> StepResult:
    plan = launch.role_plan
    if plan is not None and plan.kind == "acp":
        return await _acp(launch)
    return await _native(launch)


async def _acp(launch: StepLaunch) -> StepResult:
    from garuda.core.sessions import SessionStore
    from garuda.interfaces.runtime_cli import run_acp_task
    from garuda.workspace.no_edits import deny_all

    plan = launch.role_plan
    summary = await run_acp_task(
        launch.prompt, runtime_id=plan.runtime_id, workspace=launch.workspace,
        approval=deny_all if launch.no_edits else None, emit=lambda *_a: None,
        role_plan=plan, lease_capability=launch.capability,
    )
    meta = SessionStore().load_meta(summary["session_id"])
    return StepResult(summary["session_id"], summary.get("status") == "completed",
                      str(meta.get("final_message") or ""))


async def _native(launch: StepLaunch) -> StepResult:
    from garuda.agents import role_agent
    from garuda.agents.setup import prepare_agent_run
    from garuda.core.events import EventStore
    from garuda.interfaces.runner import run_agent_task

    plan = launch.role_plan
    permissions = "readonly" if launch.no_edits else (plan.permissions if plan else None)
    prepared = await prepare_agent_run(
        (role_agent.native_spec(plan, launch.workspace) if plan and plan.profile else "build"),
        workspace=launch.workspace,
        model=plan.model_id if plan else None,
        permission_mode=permissions,
        reasoning_effort=plan.effort if plan else None,
    )
    events = EventStore()
    result = await run_agent_task(
        task=launch.prompt, model=prepared.reasoning, agent=prepared.agent,
        tools=prepared.tools, config=prepared.config, permissions=prepared.permissions,
        workspace=launch.workspace, events=events, mcp_manager=prepared.mcp_manager,
        lease_capability=launch.capability,
        session_record={"role": plan.record()} if plan else None,
        collection_model=prepared.collection, collection_policy=prepared.collection_policy,
    )
    return StepResult(events.session_id, bool(result.success), result.final_message or "")
