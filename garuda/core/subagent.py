from dataclasses import dataclass
from pathlib import Path

from garuda.agents.loader import load_profile
from garuda.core.events import EventStore, EventType
from garuda.core.permissions import PermissionEngine
from garuda.model.protocol import Model
from garuda.tools import build_toolkit
from garuda.types import AgentResult, Message, Role
from garuda.workspace.protocol import Environment


@dataclass
class SubagentRunner:
    model: Model
    env: Environment
    events: EventStore
    agents_dir: Path | None = None
    skills_dirs: list[str] | None = None
    workspace_root: str | None = None
    max_turns: int = 50
    fork_parent_context: bool = False
    parent_messages: list[Message] | None = None

    async def run(self, profile_name: str, task: str) -> AgentResult:
        from garuda.agents.loader import resolve_system_prompt
        from garuda.core.loop import DefaultAgent

        profile = load_profile(profile_name, extra_dir=self.agents_dir)
        config = profile.to_agent_config()
        config.max_turns = min(config.max_turns, self.max_turns)
        config.enable_three_step_summary = False
        config.system_prompt = resolve_system_prompt(profile, self.workspace_root)

        permissions = PermissionEngine(
            mode=profile.permission_mode,
            tool_rules=profile.tool_rules,
            path_rules=profile.path_rules,
            bash_rules=profile.bash_rules,
        )
        tools, mcp_manager = await build_toolkit(
            profile.tools,
            profile.mcp_config_path,
        )
        sub_events = EventStore()
        agent = DefaultAgent(profile_name=profile.name)

        result = await agent.run(
            task=task,
            model=self.model,
            env=self.env,
            tools=tools,
            config=config,
            events=sub_events,
            permissions=permissions,
        )
        if mcp_manager is not None:
            await mcp_manager.close()

        self.events.append(
            EventType.USER_MESSAGE,
            {
                "content": f"[subagent:{profile_name}] {result.final_message}",
                "subagent": profile_name,
                "success": result.success,
            },
        )
        result.metadata["subagent_session_id"] = sub_events.session_id
        result.metadata["subagent_profile"] = profile_name
        return result


def format_subagent_summary(profile_name: str, result: AgentResult) -> str:
    return (
        f"Subagent @{profile_name} finished (success={result.success}, turns={result.turns}):\n"
        f"{result.final_message}"
    )
