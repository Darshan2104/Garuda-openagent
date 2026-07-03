"""High-level Software Agent SDK entry point."""

from pathlib import Path

from garuda.agents.loader import load_profile, resolve_system_prompt
from garuda.core.events import EventStore
from garuda.core.permissions import PermissionEngine
from garuda.core.rigorous import create_agent
from garuda.interfaces.runner import run_agent_task
from garuda.mcp.config import resolve_mcp_config_paths
from garuda.model.litellm_model import LitellmModel
from garuda.tools import build_toolkit, register_tool
from garuda.tools.protocol import Tool
from garuda.types import AgentResult


class SoftwareAgent:
    """OpenHands-style SDK for building on top of Garuda."""

    def __init__(
        self,
        workspace: str | Path = ".",
        model: str = "openai/gpt-4o-mini",
        agent: str = "build",
        agents_dir: str | Path | None = None,
        mcp_config: str | None = None,
        workspace_kind: str = "local",
        docker_image: str = "ubuntu:22.04",
        docker_host: str | None = None,
        mode: str = "standard",
    ):
        self.workspace = str(workspace)
        self.model_name = model
        self.agent_name = agent
        self.agents_dir = Path(agents_dir) if agents_dir else None
        self.mcp_config = mcp_config
        self.workspace_kind = workspace_kind
        self.docker_image = docker_image
        self.docker_host = docker_host
        self.mode = mode

    @staticmethod
    def register_tool(tool: Tool, *, replace: bool = False) -> None:
        """Register a custom tool available to all SDK runs."""
        register_tool(tool, replace=replace)

    async def run(
        self,
        task: str,
        *,
        events: EventStore | None = None,
        resume: str | None = None,
    ) -> AgentResult:
        """Execute a task and return the agent result.

        Pass ``resume`` (a saved session id, unique prefix, or ``"latest"``) to
        seed the run with a prior session's conversation.
        """
        profile = load_profile(self.agent_name, extra_dir=self.agents_dir)
        config = profile.to_agent_config()
        config.mode = self.mode
        config.workspace_kind = self.workspace_kind
        config.docker_image = self.docker_image
        config.docker_host = self.docker_host
        config.system_prompt = resolve_system_prompt(profile, self.workspace)
        mcp_paths = resolve_mcp_config_paths(self.workspace, self.mcp_config or config.mcp_config_path)

        model = LitellmModel(
            model_name=self.model_name,
            reasoning_effort=config.reasoning_effort,
            thinking_budget_tokens=config.thinking_budget_tokens,
        )
        permissions = PermissionEngine(
            mode=config.permission_mode,
            tool_rules=profile.tool_rules,
            path_rules=profile.path_rules,
            bash_rules=profile.bash_rules,
        )
        agent = create_agent(profile.name, mode=self.mode)
        events = events or EventStore()
        tools, mcp_manager = await build_toolkit(profile.tools, mcp_paths)

        return await run_agent_task(
            task=task,
            model=model,
            agent=agent,
            tools=tools,
            config=config,
            permissions=permissions,
            workspace=self.workspace,
            events=events,
            workspace_kind=self.workspace_kind,
            docker_image=self.docker_image,
            docker_host=self.docker_host,
            mcp_manager=mcp_manager,
            agents_dir=self.agents_dir,
            resume=resume,
        )

    def conversation(self) -> "Conversation":
        from garuda.sdk.conversation import Conversation

        return Conversation(
            workspace=self.workspace,
            model=self.model_name,
            agent=self.agent_name,
            agents_dir=self.agents_dir,
            mcp_config=self.mcp_config,
            mode=self.mode,
            workspace_kind=self.workspace_kind,
            docker_image=self.docker_image,
            docker_host=self.docker_host,
        )
