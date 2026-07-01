"""Stateful conversation wrapper around Garuda agent runs."""

from pathlib import Path

from garuda.core.events import EventStore
from garuda.interfaces.runner import run_agent_task
from garuda.interfaces.session import AgentSession
from garuda.types import AgentResult
from garuda.workspace.local import LocalEnvironment


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
    ):
        self._workspace = str(workspace)
        self._model_name = model
        self._agent_name = agent
        self._agents_dir = Path(agents_dir) if agents_dir else None
        self._mcp_config = mcp_config
        self._mode = mode
        self._session: AgentSession | None = None
        self._env = LocalEnvironment(workspace_root=self._workspace)

    async def _ensure_session(self) -> AgentSession:
        if self._session is None:
            self._session = await AgentSession.create(
                agent_name=self._agent_name,
                model=self._model_name,
                workspace=self._workspace,
                agents_dir=self._agents_dir,
                mcp_config_path=self._mcp_config,
                mode=self._mode,
            )
        return self._session

    async def run(self, task: str) -> AgentResult:
        """Run a task in this conversation."""
        session = await self._ensure_session()
        context = session.prepare_context(task)
        return await session.agent.run(
            task=task,
            model=session.model,
            env=self._env,
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

    @property
    def events(self) -> EventStore:
        if self._session is None:
            return EventStore()
        return self._session.events
