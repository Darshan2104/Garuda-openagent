"""High-level Software Agent SDK entry point."""

from pathlib import Path
from typing import TYPE_CHECKING

from garuda.agents.setup import prepare_agent_run
from garuda.core.events import EventStore
from garuda.interfaces.runner import run_agent_task
from garuda.interfaces.runtime_cli import RUNTIME_API_VERSION
from garuda.model.protocol import DEFAULT_MODEL
from garuda.tools.protocol import Tool
from garuda.types import AgentResult

if TYPE_CHECKING:
    # Only for the return-type annotation on conversation(); the runtime import
    # stays inside the method to avoid a circular import with garuda.sdk.conversation.
    from garuda.sdk.conversation import Conversation


class SoftwareAgent:
    """OpenHands-style SDK for building on top of Garuda."""

    def __init__(
        self,
        workspace: str | Path = ".",
        model: str | object = DEFAULT_MODEL,
        agent: str = "build",
        agents_dir: str | Path | None = None,
        mcp_config: str | None = None,
        workspace_kind: str = "local",
        docker_image: str = "ubuntu:22.04",
        docker_host: str | None = None,
        mode: str | None = None,
        extra_tools: list[Tool] | None = None,
        load_project_tools: bool | None = None,
        runtime: str = "native",
        store=None,
        reasoning_model: str | object | None = None,
        collection_model: str | object | None = None,
        no_collection: bool = False,
        model_binding: str | None = None,
    ):
        self.workspace = str(workspace)
        # `model` stays the legacy reasoning alias. A bare default equal to the
        # built-in resolves as "unspecified" in shared setup, so environment and
        # configured bindings are honored; strings name models, live `Model`
        # objects are kept by identity for this instance's runs.
        self.model_name = model
        self.agent_name = agent
        self.agents_dir = Path(agents_dir) if agents_dir else None
        self.mcp_config = mcp_config
        self.workspace_kind = workspace_kind
        self.docker_image = docker_image
        self.docker_host = docker_host
        self.mode = mode
        self.runtime_name = runtime
        self._store = store
        self.reasoning_model = reasoning_model
        self.collection_model = collection_model
        self.no_collection = no_collection
        self.model_binding = model_binding
        self._extra_tools: list[Tool] = list(extra_tools or [])
        self._load_project_tools = load_project_tools

    def register_tool(self, tool: Tool, *, replace: bool = False) -> None:
        """Register a custom tool for this agent's runs only.

        Scoped to this instance (not the process-global registry), so two agents
        in one process can carry different custom tools without clashing.
        """
        if not replace and any(t.name == tool.name for t in self._extra_tools):
            raise ValueError(f"Tool already registered: {tool.name}")
        self._extra_tools = [t for t in self._extra_tools if t.name != tool.name] + [tool]

    async def run(
        self,
        task: str,
        *,
        events: EventStore | None = None,
        resume: str | None = None,
    ) -> AgentResult:
        """Execute a task and return the agent result.

        Pass ``resume`` (a saved session id, unique prefix, or ``"latest"``) to
        seed the run with a prior session's conversation. Non-native runtimes
        execute one ACP turn per run through the selected harness.
        """
        if self.runtime_name != "native":
            return await self._run_acp(task)
        from garuda.config.agent_home import resolve_agents_dirs

        agents_dir = resolve_agents_dirs(self.workspace, self.agents_dir)
        prepared = await prepare_agent_run(
            self.agent_name,
            workspace=self.workspace,
            agents_dir=agents_dir,
            mcp_config_path=self.mcp_config,
            mode=self.mode,
            model=self.model_name,
            reasoning_model=self.reasoning_model,
            collection_model=self.collection_model,
            no_collection=self.no_collection,
            model_binding=self.model_binding,
            extra_tools=self._extra_tools,
            load_project_tools=self._load_project_tools,
        )
        profile, config, permissions, tools, agent, mcp_manager = prepared
        model = prepared.reasoning
        events = events or EventStore()
        config.workspace_kind = self.workspace_kind
        config.docker_image = self.docker_image
        config.docker_host = self.docker_host

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
            agents_dir=agents_dir,
            resume=resume,
        )

    async def _run_acp(self, task: str) -> AgentResult:
        """One ACP turn through the selected harness, wrapped as an AgentResult.

        The harness resolves through the shared registry (unknown and
        disabled ids are refused before anything launches) and the tenure
        attaches to a persisted unified session — selection is a session
        transition, not an in-memory choice.
        """
        from garuda.core.events import EventStore
        from garuda.core.sessions import SessionStore
        from garuda.interfaces.runtime_cli import (
            acp_adapter_for_workspace,
            attach_acp_segment,
        )
        from garuda.runtime import RegistryError

        store = self._store or SessionStore()
        events = EventStore()
        try:
            _, adapter = acp_adapter_for_workspace(
                self.workspace,
                self.runtime_name,
                store=store,
                persist_dir=str(store.session_dir(events.session_id)),
            )
        except ValueError as exc:
            if "disabled by user configuration" in str(exc):
                raise RegistryError(str(exc)) from exc
            raise
        store.begin(
            events.session_id,
            task=task,
            model=self.model_name,
            agent=self.agent_name,
            workspace=self.workspace,
        )
        store.ensure_unified(events.session_id)
        if self.workspace_kind == "local":
            try:
                from garuda.workspace.diff import capture_baseline

                store.record_baseline(
                    events.session_id, capture_baseline(self.workspace).to_dict()
                )
            except Exception:
                pass
        await adapter.start(task=task, session_id=events.session_id)
        try:
            turn = await adapter.prompt(task)
            trail, _ = await adapter.poll_events(0)
        finally:
            await adapter.close()
        texts = [
            e.payload.get("chunk", "") or e.payload.get("text", "")
            for e in trail
            if e.kind.value == "message"
        ]
        result = AgentResult(
            success=True,
            final_message="".join(t for t in texts if isinstance(t, str)),
            messages=[],
            turns=turn,
            metadata={"runtime": self.runtime_name, "api": RUNTIME_API_VERSION},
        )
        attach_acp_segment(store, events.session_id, adapter)
        store.finish(events.session_id, result)
        result.metadata["session_id"] = events.session_id
        return result

    def conversation(self) -> "Conversation":
        from garuda.sdk.conversation import Conversation

        return Conversation(
            workspace=self.workspace,
            model=self.model_name,
            reasoning_model=self.reasoning_model,
            collection_model=self.collection_model,
            no_collection=self.no_collection,
            model_binding=self.model_binding,
            agent=self.agent_name,
            agents_dir=self.agents_dir,
            mcp_config=self.mcp_config,
            mode=self.mode,
            workspace_kind=self.workspace_kind,
            docker_image=self.docker_image,
            docker_host=self.docker_host,
            runtime=self.runtime_name,
            store=self._store,
        )
