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
    if launch.require_owned:
        from garuda.runtime.owned_role import launch_owned_step

        return await launch_owned_step(launch)
    plan = launch.role_plan
    if plan is not None and plan.kind == "acp":
        return await _acp(launch)
    return await _native(launch)


async def _acp(launch: StepLaunch) -> StepResult:
    import uuid

    from garuda.acp.protocol import AcpError
    from garuda.core.sessions import SessionStore
    from garuda.flows.quota_recovery import RecoveryWaiting
    from garuda.interfaces.runtime_cli import run_acp_task
    from garuda.workspace.no_edits import deny_all

    plan = launch.role_plan
    store = launch.store or SessionStore()
    session_id = str(uuid.uuid4())
    try:
        summary = await run_acp_task(
            launch.prompt, runtime_id=plan.runtime_id, workspace=launch.workspace,
            approval=deny_all if launch.no_edits else None, emit=lambda *_a: None,
            role_plan=plan, lease_capability=launch.capability, store=store,
            no_edits=launch.no_edits, session_id=session_id,
            require_cleanup=launch.quota_recovery,
            codex_executable=launch.codex_executable,
            before_prompt=launch.quota_guard,
        )
    except RecoveryWaiting as exc:
        return StepResult(session_id, False, "", error=str(exc), quota_wait=True,
                          closure={"status": "closed"})
    except AcpError as exc:
        # The agent stopped (ended its turn, exited, timed out): a failed step
        # with its reason, not an interrupted flow.
        return StepResult(session_id, False, "", error=str(exc),
                          closure={"status": "closed"} if launch.quota_recovery else None)
    return StepResult(summary["session_id"], summary.get("status") == "completed",
                      str(summary.get("reply") or ""),
                      closure={"status": "closed"} if launch.quota_recovery else None)


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
        lease_capability=launch.capability, store=launch.store,
        session_record={"role": plan.record()} if plan else None,
        collection_model=prepared.collection, collection_policy=prepared.collection_policy,
    )
    return StepResult(events.session_id, bool(result.success), result.final_message or "")
