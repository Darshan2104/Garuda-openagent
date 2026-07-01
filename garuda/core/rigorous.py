"""Plan → execute → critic verification agent mode."""

from __future__ import annotations

from garuda.agents.loader import load_profile
from garuda.core.events import EventStore, EventType
from garuda.core.loop import DefaultAgent
from garuda.core.permissions import PermissionEngine
from garuda.core.verifier import CompletionVerifier
from garuda.model.protocol import Model
from garuda.plugins.hooks import HookRegistry
from garuda.tools import tools_for_names
from garuda.tools.protocol import Tool
from garuda.types import AgentConfig, AgentResult, Message, Role
from garuda.workspace.protocol import Environment


def create_agent(profile_name: str, mode: str = "standard") -> DefaultAgent | RigorousAgent:
    """Return the agent implementation for a profile mode."""
    if mode == "rigorous":
        return RigorousAgent(profile_name=profile_name)
    return DefaultAgent(profile_name=profile_name)


class RigorousAgent:
    """Three-phase agent: plan with read-only profile, execute, then critic review."""

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
        build_agent = DefaultAgent(profile_name=self._profile_name)
        exec_result = await build_agent.run(
            task=exec_task,
            model=model,
            env=env,
            tools=tools,
            config=config,
            events=events,
            permissions=permissions,
            hooks=hooks,
            subagent_runner=subagent_runner,
        )

        approved, feedback = await self._critic_review(task, plan_result.final_message, exec_result, model)
        events.append(EventType.VERIFICATION, {"approved": approved, "feedback": feedback, "phase": "critic"})
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
    ) -> tuple[bool, str]:
        """Run a lightweight critic pass before accepting rigorous-mode completion."""
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
                Message(
                    role=Role.USER,
                    content=(
                        f"Task:\n{task}\n\nPlan:\n{plan}\n\n"
                        f"Agent result (success={result.success}):\n{result.final_message}"
                    ),
                ),
            ]
        )
        text = (response.content or "").strip()
        if text.upper().startswith("APPROVED"):
            return True, text
        return False, text or "No critic feedback provided."
