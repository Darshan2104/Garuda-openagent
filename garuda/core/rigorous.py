"""Plan → execute → critic verification agent mode."""

from __future__ import annotations

import logging

from garuda.agents.loader import load_profile
from garuda.core.events import EventStore, EventType
from garuda.core.loop import DefaultAgent
from garuda.core.modes import apply_mode_preset, describe_config, is_rigorous
from garuda.core.permissions import PermissionEngine
from garuda.core.spend import SpendLimitExceeded, gate_for
from garuda.core.termination import TerminalStrategy
from garuda.core.verifier import CompletionVerifier, gather_git_evidence
from garuda.model import accounting
from garuda.model.protocol import Model
from garuda.plugins.hooks import HookRegistry
from garuda.tools import tools_for_names
from garuda.tools.protocol import Tool
from garuda.types import AgentConfig, AgentResult, Message, Role
from garuda.workspace.protocol import Environment

logger = logging.getLogger(__name__)

# How many times the executor is re-run with critic feedback after a rejection.
MAX_REPAIR_ROUNDS = 2


def create_agent(profile_name: str, mode: str | None = None) -> DefaultAgent | RigorousAgent:
    """Return the agent implementation for a run mode."""
    if is_rigorous(mode):
        return RigorousAgent(profile_name=profile_name)
    return DefaultAgent(profile_name=profile_name)


def _parse_critic_verdict(text: str) -> tuple[bool, str]:
    """Parse a critic reply robustly: strip markdown/whitespace, inspect the first line.

    Returns (approved, feedback). Anything that does not clearly start with
    APPROVED is treated as a rejection with the full text as feedback.
    """
    cleaned = (text or "").strip()
    first_line = ""
    for line in cleaned.splitlines():
        stripped = line.strip().strip("#*_`>").strip()
        if stripped:
            first_line = stripped
            break
    upper = first_line.upper()
    if upper.startswith("APPROVED"):
        return True, cleaned
    return False, cleaned or "No critic feedback provided."


class RigorousAgent:
    """Three-phase agent: plan with read-only profile, execute, then critic review.

    On critic rejection, the executor is re-run with the critic's feedback
    appended to the task, up to MAX_REPAIR_ROUNDS repair rounds.
    """

    def __init__(self, profile_name: str = "build"):
        self._profile_name = profile_name
        self._verifier = CompletionVerifier()

    @property
    def profile_name(self) -> str:
        return self._profile_name

    async def run(
        self,
        task: str,
        model: Model,
        env: Environment,
        tools: list[Tool],
        config: AgentConfig | None = None,
        events: EventStore | None = None,
        permissions: PermissionEngine | None = None,
        hooks: HookRegistry | None = None,
        subagent_runner=None,
        agents_dir=None,
        context=None,
        checkpoint=None,
        # These three exist on DefaultAgent.run and are passed positionally-by-name by
        # `interfaces/runner.py`, so omitting them was not a missing feature — it was a
        # TypeError on every `--mode rigorous` run. The mode could not start at all.
        # Keep this signature identical to DefaultAgent.run; `tests/test_conformance.py`
        # now compares them so the next parameter added cannot break only this path.
        buffer=None,
        emit_session_events: bool = True,
        state_checkpoint=None,
        pack_manager=None,
        pack_source_runtime: str = "native",
        pack_git_evidence: str = "",
        initial_state=None,
        workspace_delta_loader=None,
        terminal_strategy: TerminalStrategy | None = None,
        collection_model=None,
        collection_policy=None,
        allowed_tool_effects=None,
        model_delta_consumer=None,
    ) -> AgentResult:
        config = config or apply_mode_preset(AgentConfig(mode="rigorous"))
        events = events or EventStore()
        permissions = permissions or PermissionEngine(mode=config.permission_mode)
        if emit_session_events:
            # Same shape as the one `run_state.prepare_run` emits, so a trajectory
            # reader has one session_start contract rather than two. The inner plan
            # and executor runs pass emit_session_events=False, so this is the only
            # one a rigorous log carries — it has to describe the whole run.
            events.append(
                EventType.SESSION_START,
                {
                    "task": task,
                    "model": model.model_name,
                    "agent": self._profile_name,
                    "mode": "rigorous",
                    "permission_mode": permissions.mode,
                    "config": describe_config(config),
                    **(
                        {"diagnostics": list(config.prompt_diagnostics)}
                        if getattr(config, "prompt_diagnostics", None)
                        else {}
                    ),
                },
            )

        # One run-tree gate for every phase: created here, before the plan, so the
        # plan run, each build round (prepare_run reuses it) and the critic calls
        # all admit against the same budget. The critic is an auxiliary call, so
        # it is bound here for admission and for its model_response record.
        gate = gate_for(config)
        accounting_token = accounting.bind(events, gate)
        try:
            plan_profile = load_profile("plan")
            plan_config = plan_profile.to_agent_config()
            plan_config.max_turns = min(max(config.max_turns // 4, 5), 30)
            plan_config.enable_verifier = False
            plan_config.max_run_tokens = config.max_run_tokens
            plan_config.max_run_cost_usd = config.max_run_cost_usd
            plan_config.spend_gate = gate
            plan_tools = tools_for_names(plan_profile.tools)
            plan_permissions = PermissionEngine(mode=plan_profile.permission_mode, tool_rules=plan_profile.tool_rules)

            plan_agent = DefaultAgent(profile_name="plan")
            plan_result = await plan_agent.run(
                task=f"Create a detailed step-by-step plan for this task:\n{task}",
                model=model,
                env=env,
                tools=plan_tools,
                config=plan_config,
                events=events,
                permissions=plan_permissions,
                hooks=hooks,
                emit_session_events=False,  # rigorous emits its own single session span
                model_delta_consumer=model_delta_consumer,
            )
            events.append(EventType.USER_MESSAGE, {"content": f"[rigorous:plan] {plan_result.final_message}"})

            if not plan_result.success:
                if plan_result.metadata.get("spend_limit"):
                    self._end_on_spend(events, emit_session_events, plan_result)
                elif emit_session_events:
                    events.append(
                        EventType.SESSION_END, {"success": False, "reason": "plan_failed"}
                    )
                return plan_result

            exec_task = f"{task}\n\n## Approved plan\n{plan_result.final_message}"
            current_task = exec_task
            exec_result: AgentResult | None = None
            approved = False
            feedback = ""

            for attempt in range(MAX_REPAIR_ROUNDS + 1):
                build_agent = DefaultAgent(profile_name=self._profile_name)
                # run() only seeds the task when it creates the context itself, so with a
                # threaded-in context (resume / multi-turn chat / SDK) every round must
                # append its effective task explicitly. Round 0 carries the approved plan
                # and later rounds carry the critic feedback — skipping either leaves the
                # executor working from history alone, blind to the plan it just paid for.
                if context is not None:
                    context.append(Message(role=Role.USER, content=current_task))
                exec_result = await build_agent.run(
                    task=current_task,
                    model=model,
                    env=env,
                    tools=tools,
                    config=config,
                    events=events,
                    permissions=permissions,
                    hooks=hooks,
                    subagent_runner=subagent_runner,
                    agents_dir=agents_dir,
                    context=context,
                    checkpoint=checkpoint,
                    # The executor is the phase that does the work, so it is the one that
                    # gets the session buffer (archived tool output stays retrievable
                    # across repair rounds) and the working-state checkpoint.
                    buffer=buffer,
                    state_checkpoint=state_checkpoint,
                    emit_session_events=False,  # rigorous emits its own single session span
                    pack_manager=pack_manager,
                    pack_source_runtime=pack_source_runtime,
                    pack_git_evidence=pack_git_evidence,
                    initial_state=initial_state,
                    workspace_delta_loader=workspace_delta_loader,
                    terminal_strategy=terminal_strategy,
                    collection_model=collection_model,
                    collection_policy=collection_policy,
                    allowed_tool_effects=allowed_tool_effects,
                    model_delta_consumer=model_delta_consumer,
                )

                if exec_result.metadata.get("spend_limit"):
                    # The budget is gone: a critic call would only be refused, and
                    # its rejection would hide why the run stopped.
                    self._end_on_spend(events, emit_session_events, exec_result)
                    return exec_result
                try:
                    approved, feedback = await self._critic_review(
                        task, plan_result.final_message, exec_result, model, env
                    )
                except SpendLimitExceeded as exc:
                    what = "cannot be enforced" if exc.cause == "cost_unknown" else "exhausted"
                    exec_result.success = False
                    exec_result.final_message = f"Run-tree spend budget {what}: {exc}"
                    exec_result.metadata["spend_limit"] = exc.cause
                    self._end_on_spend(events, emit_session_events, exec_result)
                    return exec_result
                events.append(
                    EventType.VERIFICATION,
                    {"approved": approved, "feedback": feedback, "phase": "critic", "attempt": attempt},
                )
                if approved:
                    break
                current_task = f"{exec_task}\n\n## Critic feedback on previous attempt\n{feedback}"

            if not approved:
                if emit_session_events:
                    events.append(
                        EventType.SESSION_END, {"success": False, "reason": "critic_rejected"}
                    )
                exec_result.success = False
                exec_result.final_message = f"Critic rejected completion: {feedback}"
                return exec_result

            if emit_session_events:
                events.append(
                    EventType.SESSION_END, {"success": exec_result.success, "mode": "rigorous"}
                )
            return exec_result
        finally:
            accounting.reset(accounting_token)

    @staticmethod
    def _end_on_spend(events: EventStore, emit_session_events: bool, result: AgentResult) -> None:
        """The run tree's spend gate refused a phase's call: the run ends there."""
        if emit_session_events:
            events.append(
                EventType.SESSION_END,
                {"success": False, "reason": "spend_limit",
                 "spend_cause": result.metadata["spend_limit"], "error": result.final_message},
            )

    async def _critic_review(
        self,
        task: str,
        plan: str,
        result: AgentResult,
        model: Model,
        env: Environment,
    ) -> tuple[bool, str]:
        """Run a lightweight critic pass before accepting rigorous-mode completion."""
        git_evidence = await gather_git_evidence(env)
        user_parts = [
            f"Task:\n{task}",
            f"Plan:\n{plan}",
            f"Agent result (success={result.success}):\n{result.final_message}",
        ]
        if git_evidence:
            user_parts.append(f"Git evidence from the workspace:\n{git_evidence}")
        try:
            response = await accounting.tagged(model, "critic").complete(
                [
                    Message(
                        role=Role.SYSTEM,
                        content=(
                            "You are a critical reviewer for a software engineering agent. "
                            "Decide if the work satisfies the task. Reply with APPROVED or "
                            "REJECTED: <reason>."
                        ),
                    ),
                    Message(role=Role.USER, content="\n\n".join(user_parts)),
                ]
            )
        except SpendLimitExceeded:
            # Not an unreachable critic: the run's budget refused the call.
            raise
        except Exception as exc:
            # A critic that can't be reached must not crash the whole rigorous run
            # (discarding a completed execution). Fail closed: treat as not-approved.
            logger.warning("Critic model call failed: %s", exc, exc_info=True)
            return False, f"critic could not be reached ({type(exc).__name__})"
        return _parse_critic_verdict(response.content or "")
