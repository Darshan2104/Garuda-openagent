"""Start one flow step as its own session (plan task C.6a, #158).

A native step runs through ``run_agent_task`` with the role's profile, exact
model, effort and permissions; an ACP step through ``run_acp_task`` with the
role's proven options. Both borrow the flow's lease through the step's
capability, so neither takes or keeps the workspace itself. A ``no-edits``
step runs native ``readonly`` or in ACP Docker confinement.

A step with ``limits`` (#828) passes its deadline to the launcher's own
deadline and, for ACP, asks the adapter's silence watchdog to watch the turn;
the step's result names which one stopped it (``liveness``). A step without
``limits`` launches exactly as before.

An ACP step with a turn spec (``launch.turns``: declared outputs or
``long_run``) passes :class:`garuda.flows.long_step.StepTurns` as
``run_acp_task``'s ``after_turn`` hook: one repair turn for bad output blocks
(#819) and, with ``long_run``, the turn loop. The step's output is then the
reply the hook chose, and its result carries the hook's record (``turns``).
"""

from __future__ import annotations

import time

from garuda.flows.engine import StepLaunch, StepResult


async def launch_step(launch: StepLaunch) -> StepResult:
    if launch.require_owned:
        from garuda.runtime.owned_role import launch_owned_step

        return await launch_owned_step(launch)
    plan = launch.role_plan
    if plan is not None and plan.kind == "acp":
        return await _acp(launch)
    return await _native(launch)


def _link(launch: StepLaunch, store, session_id: str) -> dict | None:
    """Link the flow to this attempt's step session before its receipt exists.

    Writes the attempt's launch record (:mod:`garuda.flows.launches`) and
    returns the ``flow_step`` record the step session carries from its
    creation. A parallel member (``step:role``) is linked by its receipt only.
    """
    import datetime as _dt

    from garuda.flows import launches

    if ":" in launch.step_id or not launch.flow_session:
        return None
    try:
        flow_dir = store.session_dir(launch.flow_session) / "flow"
    except (OSError, ValueError, AttributeError, TypeError):
        return None  # a store without session directories (tests): nothing to link
    launches.write(flow_dir, launch.step_id, launch.attempt, {
        "session_id": session_id,
        "started_at": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")})
    return {"flow_step": {"flow_session": launch.flow_session, "step": launch.step_id,
                          "attempt": launch.attempt}}


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
    linked = _link(launch, store, session_id)
    hook = None
    if launch.turns is not None:
        from garuda.flows.long_step import StepTurns

        hook = StepTurns(launch.turns, step_prompt=launch.prompt, store=store,
                         workspace=launch.workspace, session_id=session_id)
    try:
        summary = await run_acp_task(
            launch.prompt, runtime_id=plan.runtime_id, workspace=launch.workspace,
            approval=deny_all if launch.no_edits else None, emit=lambda *_a: None,
            role_plan=plan, lease_capability=launch.capability, store=store,
            no_edits=launch.no_edits, session_id=session_id,
            require_cleanup=launch.quota_recovery,
            codex_executable=launch.codex_executable,
            before_prompt=launch.quota_guard,
            **acp_limits(launch.limits),
            **({"after_turn": hook} if hook is not None else {}),
            **({"session_record": linked} if linked is not None else {}),
        )
    except RecoveryWaiting as exc:
        return StepResult(session_id, False, "", error=_redacted(exc), quota_wait=True,
                          closure={"status": "closed"})
    except AcpError as exc:
        # The agent stopped (ended its turn, exited, timed out): a failed step
        # with its reason, not an interrupted flow. The reason (remote error
        # data, stderr tail) is agent-supplied and lands in the step receipt,
        # so it is redacted here too (#729).
        return StepResult(session_id, False, "", error=_redacted(exc),
                          closure={"status": "closed"} if launch.quota_recovery else None,
                          liveness=liveness_of(exc, launch.limits),
                          stop_reason=getattr(exc, "acp_stop_reason", None),
                          turns=hook.record() if hook is not None else None)
    if hook is None:
        return StepResult(summary["session_id"], summary.get("status") == "completed",
                          str(summary.get("reply") or ""),
                          closure={"status": "closed"} if launch.quota_recovery else None,
                          stop_reason=summary.get("stop_reason"))
    return StepResult(summary["session_id"],
                      summary.get("status") == "completed" and hook.stop is None, hook.output,
                      closure={"status": "closed"} if launch.quota_recovery else None,
                      error=(hook.stop or {}).get("message", ""),
                      stop_reason=summary.get("stop_reason"), turns=hook.record())


def acp_limits(limits) -> dict:
    """``run_acp_task`` keywords for a step's limits (#828); none without limits."""
    if limits is None:
        return {}
    return {**({"deadline_sec": limits.deadline_sec} if limits.deadline_sec else {}),
            **({"silence_watchdog": limits.watchdog} if limits.watchdog else {})}


def liveness_of(exc: BaseException, limits) -> str | None:
    """``"deadline"`` or ``"silence"`` when the step's own limit ended its run (#828)."""
    kind = getattr(exc, "liveness", None)
    return kind if limits is not None and kind in ("deadline", "silence") else None


def _redacted(exc: BaseException) -> str:
    from garuda.context.redact import redact_text

    return redact_text(str(exc))[0]


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
    deadline = launch.limits.deadline_sec if launch.limits is not None else None
    if deadline:
        prepared.config.deadline_sec = min(prepared.config.deadline_sec or deadline, deadline)
    events = EventStore()
    from garuda.core.sessions import SessionStore

    linked = _link(launch, launch.store or SessionStore(), events.session_id)
    started = time.monotonic()
    result = await run_agent_task(
        task=launch.prompt, model=prepared.reasoning, agent=prepared.agent,
        tools=prepared.tools, config=prepared.config, permissions=prepared.permissions,
        workspace=launch.workspace, events=events, mcp_manager=prepared.mcp_manager,
        lease_capability=launch.capability, store=launch.store,
        session_record=({**({"role": plan.record()} if plan else {}), **(linked or {})}
                        or None),
        collection_model=prepared.collection, collection_policy=prepared.collection_policy,
    )
    return StepResult(events.session_id, bool(result.success), result.final_message or "",
                      liveness=native_liveness(result.success, started, deadline))


def native_liveness(success, started: float, deadline: float | None) -> str | None:
    """The native loop stops at its deadline with a failed result; name it (#828)."""
    return "deadline" if deadline and not success and time.monotonic() - started >= deadline else None
