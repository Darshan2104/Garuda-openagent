from dataclasses import dataclass
from pathlib import Path

from garuda.agents.loader import load_profile
from garuda.core.events import EventStore
from garuda.core.permissions import PermissionEngine
from garuda.model.protocol import Model
from garuda.tools import tools_for_names
from garuda.types import AgentResult
from garuda.workspace.protocol import Environment


@dataclass
class SubagentRunner:
    model: Model
    env: Environment
    permissions: PermissionEngine
    events: EventStore
    agents_dir: Path | None = None
    max_turns: int = 50

    async def run(self, profile_name: str, task: str) -> AgentResult:
        from garuda.core.loop import DefaultAgent

        profile = load_profile(profile_name, extra_dir=self.agents_dir)
        config = profile.to_agent_config()
        config.max_turns = min(config.max_turns, self.max_turns)
        config.enable_three_step_summary = False
        tools = tools_for_names(profile.tools)
        agent = DefaultAgent(profile_name=profile.name)
        return await agent.run(
            task=task,
            model=self.model,
            env=self.env,
            tools=tools,
            config=config,
            events=self.events,
            permissions=self.permissions,
        )


def format_subagent_summary(profile_name: str, result: AgentResult) -> str:
    return (
        f"Subagent @{profile_name} finished (success={result.success}, turns={result.turns}):\n"
        f"{result.final_message}"
    )
