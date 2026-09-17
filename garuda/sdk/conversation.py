"""Stateful conversation wrapper around Garuda agent runs."""

from pathlib import Path
from typing import Any

from garuda.core.events import EventStore
from garuda.interfaces.runner import cleanup_workspace, resolve_environment
from garuda.interfaces.session import AgentSession
from garuda.model.protocol import DEFAULT_MODEL
from garuda.types import AgentResult
from garuda.workspace.protocol import Environment


class Conversation:
    """Multi-turn Garuda session with shared event history and LLM context."""

    def __init__(
        self,
        workspace: str | Path = ".",
        model: str = DEFAULT_MODEL,
        agent: str = "build",
        agents_dir: str | Path | None = None,
        mcp_config: str | None = None,
        mode: str = "standard",
        workspace_kind: str = "local",
        docker_image: str = "ubuntu:22.04",
        docker_host: str | None = None,
        runtime: str = "native",
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
        self._runtime_name = runtime
        self._session: AgentSession | None = None
        self._env: Environment | None = None
        self._env_handle: object | None = None
        self._acp: Any | None = None
        self._acp_trail: list = []

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
        if self._runtime_name != "native":
            return await self._run_acp(task)
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

    async def _run_acp(self, task: str) -> AgentResult:
        """One turn on the held ACP runtime; the harness keeps the session."""
        from garuda.acp.catalog import adapter_for_manifest, discover, require_acp_argv
        from garuda.config.agent_home import resolve_agent_home
        from garuda.interfaces.runtime_cli import (
            RUNTIME_API_VERSION,
            load_configured_manifest_dicts,
        )
        from garuda.runtime.registry import parse_global_manifests

        if self._acp is None:
            home = resolve_agent_home(self._workspace)
            manifests = {
                m.runtime_id: m
                for m in parse_global_manifests(
                    load_configured_manifest_dicts(home.global_settings),
                    source="sdk runtimes",
                )
            }
            if self._runtime_name not in manifests:
                raise ValueError(
                    f"Unknown runtime {self._runtime_name!r}. "
                    "Configure it as a global harness manifest."
                )
            manifest = manifests[self._runtime_name]
            found = {d.runtime_id: d for d in discover([manifest])}
            entry = found.get(manifest.runtime_id)
            argv = require_acp_argv(
                manifest, executable=entry.executable if entry else None
            )
            self._acp = adapter_for_manifest(manifest, argv_override=list(argv))
            await self._acp.start(task=task)
        turn = await self._acp.prompt(task)
        events, _ = await self._acp.poll_events(len(self._acp_trail))
        self._acp_trail.extend(events)
        texts = [
            e.payload.get("chunk", "") or e.payload.get("text", "")
            for e in events
            if e.kind.value == "message"
        ]
        return AgentResult(
            success=True,
            final_message="".join(t for t in texts if isinstance(t, str)),
            messages=[],
            turns=turn,
            metadata={"runtime": self._runtime_name, "api": RUNTIME_API_VERSION},
        )

    async def close(self) -> None:
        if self._acp is not None:
            await self._acp.close()
            self._acp = None
            self._acp_trail = []
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

    def trail(self) -> list:
        """Normalized runtime events so far (ACP conversations)."""
        return list(self._acp_trail)
