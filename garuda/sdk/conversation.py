"""Stateful conversation wrapper around Garuda agent runs."""

from pathlib import Path

from garuda.agents.loader import load_profile, resolve_system_prompt
from garuda.core.events import EventStore
from garuda.core.loop import DefaultAgent
from garuda.core.permissions import PermissionEngine
from garuda.core.rigorous import create_agent
from garuda.model.litellm_model import LitellmModel
from garuda.tools import build_toolkit
from garuda.types import AgentConfig, AgentResult
from garuda.workspace.local import LocalEnvironment


class Conversation:
    """Multi-turn Garuda session with shared event history."""

    def __init__(
        self,
        workspace: str | Path = ".",
        model: str = "openai/gpt-4o-mini",
        agent: str = "build",
        agents_dir: str | Path | None = None,
        mcp_config: str | None = None,
        mode: str = "standard",
    ):
        self._workspace = str(workspace)
        self._model = LitellmModel(model_name=model)
        self._agent_name = agent
        self._agents_dir = Path(agents_dir) if agents_dir else None
        self._mcp_config = mcp_config
        self._mode = mode
        self._events = EventStore()
        self._env = LocalEnvironment(workspace_root=self._workspace)
        self._tools = None
        self._mcp_manager = None
        self._profile = load_profile(agent, extra_dir=self._agents_dir)

    async def _ensure_tools(self) -> None:
        if self._tools is None:
            config = self._profile.to_agent_config()
            mcp_path = self._mcp_config or config.mcp_config_path
            self._tools, self._mcp_manager = await build_toolkit(self._profile.tools, mcp_path)

    async def run(self, task: str) -> AgentResult:
        """Run a task in this conversation."""
        await self._ensure_tools()
        config = self._profile.to_agent_config()
        config.mode = self._mode
        config.system_prompt = resolve_system_prompt(self._profile, self._workspace)
        permissions = PermissionEngine(
            mode=config.permission_mode,
            tool_rules=self._profile.tool_rules,
            path_rules=self._profile.path_rules,
            bash_rules=self._profile.bash_rules,
        )
        agent = create_agent(self._profile.name, mode=self._mode)
        return await agent.run(
            task=task,
            model=self._model,
            env=self._env,
            tools=self._tools or [],
            config=config,
            events=self._events,
            permissions=permissions,
            agents_dir=self._agents_dir,
        )

    async def close(self) -> None:
        if self._mcp_manager is not None:
            await self._mcp_manager.close()
            self._mcp_manager = None

    @property
    def events(self) -> EventStore:
        return self._events
