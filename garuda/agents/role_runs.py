"""Owned role runs for dashboard run-kind starters (#316).

A run-kind starter (``run-with-role``, ``ask-role``) executes as one owned
parent with exactly one role step. The parent is the flow engine's proven
lifecycle: workspace lease, exclusive parent reservation, strict operation
publication, source revalidation, no-edits withholding, acceptance checks and
quarantine. The step's child is the shared owned native/ACP role executor
(``runtime/owned_role.py``), so ACP never travels through ``LiveChat`` and no
CLI output or terminal approval prompt is involved.
"""

from __future__ import annotations

from garuda.core.sessions import SessionStore


def role_run_flow(plan) -> dict:
    """The single internal step a run-kind plan executes; never user-authored."""
    no_edits = bool(plan.provenance.get("no_edits"))
    step = {"id": "ask" if no_edits else "run", "role": plan.target}
    if no_edits:
        step["write_policy"] = "no-edits"
    return {"steps": [step]}


class RoleRunService:
    """Execute an admitted run-kind starter through the owned parent path."""

    def __init__(self, store: SessionStore | None = None):
        self.store = store if store is not None else SessionStore()

    async def run_prepared(self, plan, context, *, run_session: str,
                           operation_binding: dict, on_admitted, approval_handler,
                           permission_mode: str, background_admission=None):
        from garuda.flows import engine
        from garuda.flows.service import FlowExecutionService
        from garuda.scenarios.compile import LaunchContext
        from garuda.scenarios.types import LaunchPlan

        if (not isinstance(plan, LaunchPlan) or not isinstance(context, LaunchContext)
                or plan.kind != "run" or context.kind != "run" or context.flow is not None
                or plan.target != context.target
                or set(context.role_plans) != {plan.target}):
            raise engine.FlowStopped("flow.prepared_invalid",
                                     "the admitted run plan and role context disagree")
        # Configured and explicit checks were resolved into ``context`` at
        # compile time; the engine freezes and runs them as it does for flows.
        return await FlowExecutionService(self.store)._run_owned(
            plan, context, flow=role_run_flow(plan), name=f"run:{plan.target}",
            flow_session=run_session, operation_binding=operation_binding,
            on_admitted=on_admitted, approval_handler=approval_handler,
            permission_mode=permission_mode, checks=(), launch_kind="run",
            session_name=plan.options.get("name"), background_admission=background_admission)
