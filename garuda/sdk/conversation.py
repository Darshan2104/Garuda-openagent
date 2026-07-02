"""Stateful conversation wrapper around Garuda agent runs."""

from pathlib import Path

from garuda.core.events import EventStore
from garuda.interfaces.runner import cleanup_workspace, resolve_environment
from garuda.interfaces.session import AgentSession
from garuda.types import AgentResult
from garuda.workspace.protocol import Environment


class Conversation:
    """Multi-turn Garuda session with shared event history and LLM context."""

    def __init__(
        self,
        workspace: str | Path = ".",
        model: str = "openai/gpt-4o-mini",
        agent: str = "build",
        agents_dir: str | Path | None = None,
        mcp_config: str | None = None,
        mode: str = "standard",
        workspace_kind: str = "local",
        docker_image: str = "ubuntu:22.04",
        docker_host: str | None = None,
    ):
        self._workspace = str(workspace)
        self._model_name = model
        self._agent_name = agent
        self._agents_dir = Path(agents_dir) if agents_dir else None
        self._mcp_config = mcp_config
        self._mode = mode
        self._workspace_kind = workspace_kind
        self._docker_image = docker_image
        self._docker_host = docker_host
        self._session: AgentSession | None = None
        self._env: Environment | None = None
        self._env_handle: object | None = None

    async def _ensure_session(self) -> AgentSession:
        if self._session is None:
            self._session = await AgentSession.create(
                agent_name=self._agent_name,
                model=self._model_name,
                workspace=self._workspace,
                agents_dir=self._agents_dir,
                mcp_config_path=self._mcp_config,
                mode=self._mode,
                workspace_kind=self._workspace_kind,
                docker_image=self._docker_image,
                docker_host=self._docker_host,
            )
        return self._session

    async def _ensure_env(self) -> Environment:
        if self._env is None:
            self._env, self._env_handle = await resolve_environment(
                self._workspace_kind,
                self._workspace,
                self._docker_image,
                docker_host=self._docker_host,
            )
        return self._env

    async def run(self, task: str) -> AgentResult:
        """Run a task in this conversation."""
        session = await self._ensure_session()
        env = await self._ensure_env()
        context = session.prepare_context(task)
        return await session.agent.run(
            task=task,
            model=session.model,
            env=env,
            tools=session.tools,
            config=session.config,
            events=session.events,
            permissions=session.permissions,
            agents_dir=session.agents_dir,
            context=context,
        )

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None
        if self._env_handle is not None:
            await cleanup_workspace(self._env_handle)
            self._env = None
            self._env_handle = None

    @property
    def events(self) -> EventStore:
        if self._session is None:
            return EventStore()
        return self._session.events
