"""Shared agent session state for multi-turn CLI and SDK conversations."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable

from garuda.agents.loader import AgentProfile, load_profile, resolve_system_prompt
from garuda.context.manager import ContextManager
from garuda.core.events import EventStore
from garuda.core.permissions import PermissionEngine
from garuda.core.rigorous import create_agent
from garuda.mcp.config import resolve_mcp_config_paths
from garuda.model.litellm_model import LitellmModel
from garuda.tools import build_toolkit
from garuda.types import AgentConfig, Message, Role

ApprovalHandler = Callable[[str], Awaitable[bool]]


@dataclass
class AgentSession:
    """Holds tools, permissions, events, and conversation context across turns."""

    profile: AgentProfile
    config: AgentConfig
    model: LitellmModel
    permissions: PermissionEngine
    tools: list
    agent: object
    events: EventStore = field(default_factory=EventStore)
    mcp_manager: object | None = None
    agents_dir: Path | None = None
    context: ContextManager | None = None

    @classmethod
    async def create(
        cls,
        *,
        agent_name: str,
        model: str,
        workspace: str,
        agents_dir: Path | None = None,
        mcp_config_path: str | None = None,
        mode: str | None = None,
        approval_handler: ApprovalHandler | None = None,
        workspace_kind: str = "local",
        docker_image: str = "ubuntu:22.04",
        docker_host: str | None = None,
    ) -> "AgentSession":
        from garuda.config.agent_home import resolve_agents_dir

        agents_dir = resolve_agents_dir(workspace, agents_dir)
        profile = load_profile(agent_name, extra_dir=agents_dir)
        config = profile.to_agent_config()
        if mode:  # else honor the profile's own mode
            config.mode = mode
        config.workspace_kind = workspace_kind
        config.docker_image = docker_image
        config.docker_host = docker_host
        config.system_prompt = resolve_system_prompt(profile, workspace)
        mcp_paths = resolve_mcp_config_paths(workspace, mcp_config_path or config.mcp_config_path)
        permissions = PermissionEngine(
            mode=profile.permission_mode,
            tool_rules=profile.tool_rules,
            path_rules=profile.path_rules,
            bash_rules=profile.bash_rules,
            approval_handler=approval_handler,
        )
        tools, mcp_manager = await build_toolkit(profile.tools, mcp_paths)
        return cls(
            profile=profile,
            config=config,
            model=LitellmModel(
                model_name=model,
                reasoning_effort=config.reasoning_effort,
                thinking_budget_tokens=config.thinking_budget_tokens,
            ),
            permissions=permissions,
            tools=tools,
            mcp_manager=mcp_manager,
            agents_dir=agents_dir,
            agent=create_agent(profile.name, mode=config.mode),
        )

    def prepare_context(self, task: str) -> ContextManager:
        """Seed or extend the shared LLM context for a new user turn."""
        if self.context is None:
            self.context = ContextManager(
                model=self.model,
                max_output_bytes=self.config.max_output_bytes,
                proactive_threshold=self.config.proactive_summarize_threshold,
                max_context_tokens=self.config.max_context_tokens,
                enable_three_step_summary=self.config.enable_three_step_summary,
                task=task,
            )
            self.context.seed(
                [
                    Message(role=Role.SYSTEM, content=self.config.system_prompt or ""),
                    Message(role=Role.USER, content=task),
                ]
            )
        else:
            self.context.append(Message(role=Role.USER, content=task))
        return self.context

    async def close(self) -> None:
        if self.mcp_manager is not None:
            await self.mcp_manager.close()
            self.mcp_manager = None
