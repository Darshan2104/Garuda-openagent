from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path

from garuda.agents.loader import load_profile, resolve_system_prompt
from garuda.context.manager import ContextManager
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
    parent_context: ContextManager | None = None

    def _parent_snapshot(self) -> list[Message] | None:
        """Live view of the parent conversation at invoke time, not construction time."""
        if self.parent_context is not None:
            return self.parent_context.get_messages()
        return self.parent_messages

    async def run(
        self,
        profile_name: str,
        task: str,
        *,
        fork_parent_context: bool | None = None,
    ) -> AgentResult:
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

        use_fork = self.fork_parent_context if fork_parent_context is None else fork_parent_context
        parent_snapshot = self._parent_snapshot()
        context: ContextManager | None = None
        if use_fork and parent_snapshot:
            context = ContextManager(
                model=self.model,
                max_output_bytes=config.max_output_bytes,
                proactive_threshold=config.proactive_summarize_threshold,
                max_context_tokens=config.max_context_tokens,
                enable_three_step_summary=False,
                task=task,
            )
            context.seed(deepcopy(parent_snapshot))
            context.append(
                Message(
                    role=Role.USER,
                    content=f"[subagent:{profile_name}] {task}",
                )
            )

        try:
            result = await agent.run(
                task=task,
                model=self.model,
                env=self.env,
                tools=tools,
                config=config,
                events=sub_events,
                permissions=permissions,
                context=context,
            )
        finally:
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
