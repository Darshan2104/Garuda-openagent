"""Plan → execute → critic verification agent mode."""

from __future__ import annotations

from garuda.agents.loader import load_profile
from garuda.core.events import EventStore, EventType
from garuda.core.loop import DefaultAgent
from garuda.core.permissions import PermissionEngine
from garuda.core.verifier import CompletionVerifier, gather_git_evidence
from garuda.model.protocol import Model
from garuda.plugins.hooks import HookRegistry
from garuda.tools import tools_for_names
from garuda.tools.protocol import Tool
from garuda.types import AgentConfig, AgentResult, Message, Role
from garuda.workspace.protocol import Environment

# How many times the executor is re-run with critic feedback after a rejection.
MAX_REPAIR_ROUNDS = 2


def create_agent(profile_name: str, mode: str = "standard") -> DefaultAgent | RigorousAgent:
    """Return the agent implementation for a profile mode."""
    if mode == "rigorous":
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
    ) -> AgentResult:
        config = config or AgentConfig(mode="rigorous")
        events = events or EventStore()
        permissions = permissions or PermissionEngine(mode=config.permission_mode)
        events.append(EventType.SESSION_START, {"task": task, "mode": "rigorous", "model": model.model_name})

        plan_profile = load_profile("plan")
        plan_config = plan_profile.to_agent_config()
        plan_config.max_turns = min(max(config.max_turns // 4, 5), 30)
        plan_config.enable_verifier = False
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
        )
        events.append(EventType.USER_MESSAGE, {"content": f"[rigorous:plan] {plan_result.final_message}"})

        if not plan_result.success:
            events.append(EventType.SESSION_END, {"success": False, "reason": "plan_failed"})
            return plan_result

        exec_task = f"{task}\n\n## Approved plan\n{plan_result.final_message}"
        current_task = exec_task
        exec_result: AgentResult | None = None
        approved = False
        feedback = ""

        for attempt in range(MAX_REPAIR_ROUNDS + 1):
            build_agent = DefaultAgent(profile_name=self._profile_name)
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
            )

            approved, feedback = await self._critic_review(
                task, plan_result.final_message, exec_result, model, env
            )
            events.append(
                EventType.VERIFICATION,
                {"approved": approved, "feedback": feedback, "phase": "critic", "attempt": attempt},
            )
            if approved:
                break
            current_task = f"{exec_task}\n\n## Critic feedback on previous attempt\n{feedback}"

        if not approved:
            events.append(EventType.SESSION_END, {"success": False, "reason": "critic_rejected"})
            exec_result.success = False
            exec_result.final_message = f"Critic rejected completion: {feedback}"
            return exec_result

        events.append(EventType.SESSION_END, {"success": exec_result.success, "mode": "rigorous"})
        return exec_result

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
        response = await model.complete(
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
        return _parse_critic_verdict(response.content or "")
