"""High-level Software Agent SDK entry point."""

from pathlib import Path
from typing import TYPE_CHECKING

from garuda.agents.loader import load_profile  # noqa: F401
from garuda.agents.setup import prepare_agent_run
from garuda.agents.spec_api import AgentSpec, coerce
from garuda.core.events import EventStore
from garuda.interfaces.runner import run_agent_task
from garuda.interfaces.runtime_cli import RUNTIME_API_VERSION
from garuda.model.protocol import DEFAULT_MODEL
from garuda.tools.protocol import Tool
from garuda.types import AgentResult

if TYPE_CHECKING:
    # Only for the return-type annotation on conversation(); the runtime import
    # stays inside the method to avoid a circular import with garuda.sdk.conversation.
    from garuda.model.streaming import DeltaConsumer
    from garuda.sdk.conversation import Conversation


class SoftwareAgent:
    """OpenHands-style SDK for building on top of Garuda."""

    def __init__(
        self,
        workspace: str | Path = ".",
        model: str | object = DEFAULT_MODEL,
        agent: "str | AgentSpec | dict" = "build",
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
        # A mapping or a spec is resolved now and frozen: this instance (and any
        # conversation it opens) runs one definition, whatever happens to files later.
        # A name is looked up at run time, as it always was.
        if isinstance(agent, (dict, AgentSpec)):
            agent = coerce(agent, self.workspace, agents_dir)
        self._agent = agent
        self.agent_name = agent.name if isinstance(agent, AgentSpec) else agent
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
        resume_all_projects: bool = False,
        fork: str | None = None,
        fork_at: int | None = None,
        on_delta: "DeltaConsumer | None" = None,
        subagent_deltas: bool = False,
    ) -> AgentResult:
        """Execute a task and return the agent result.

        Pass ``resume`` (a saved session id, unique prefix, or ``"latest"``) to
        seed the run with a prior session's conversation. ``"latest"`` is this
        workspace's newest session unless ``resume_all_projects`` is true. Non-native runtimes
        execute one ACP turn per run through the selected harness; registered
        tools cannot reach a harness, so such a run refuses them with
        ``ToolsUnsupportedOnRuntime`` before it starts, and a ``model``,
        ``reasoning_model``, ``collection_model``, ``model_binding``,
        ``mcp_config``, non-default ``agent`` or a ``mode`` other than
        ``standard``/``interactive`` with ``OptionsUnsupportedOnRuntime``. An ACP result's
        ``success`` means the harness ended its turn, not that Garuda verified it.
        ``fork`` with ``fork_at`` starts a native context-only fork through an
        inclusive zero-based saved message index; files/processes are not restored.

        ``on_delta`` (native runtime only) receives the live ``LiveDelta``
        fragments the loop forwards while each model call streams; sync and async
        callables are accepted. The result still comes from the assembled model
        response, and a consumer that raises is detached for that call (recorded
        as ``live_consumer_error``) without changing the run's outcome.
        ``subagent_deltas=True`` requires ``on_delta`` and opts into child
        deltas labelled with ``source`` (session id), ``agent`` and ``depth``.
        Group by source: concurrent workers interleave by scheduling, preserving
        order within each source. A labelled ``discard`` clears only that source.
        Consult and collection children are excluded; the default is parent only.
        """
        from garuda.core.session_fork import validate_fork_request

        if subagent_deltas and on_delta is None:
            raise ValueError("subagent_deltas requires on_delta")
        if on_delta is not None and self.runtime_name != "native":
            raise ValueError("live deltas are only available for the native runtime")
        validate_fork_request(fork=fork, fork_at=fork_at, resume=resume)
        if fork is not None and self.runtime_name != "native":
            raise ValueError("context fork requires the native runtime")
        if self.runtime_name != "native":
            from garuda.sdk.errors import refuse_unservable_options, refuse_unservable_tools

            refuse_unservable_tools(self.runtime_name, self._extra_tools, self._load_project_tools)
            refuse_unservable_options(
                self.runtime_name, model=self.model_name, agent=self._agent,
                reasoning_model=self.reasoning_model, collection_model=self.collection_model,
                model_binding=self.model_binding, mode=self.mode,
                mcp_config=self.mcp_config)
            return await self._run_acp(task)
        from garuda.config.agent_home import resolve_agents_dirs

        agents_dir = resolve_agents_dirs(self.workspace, self.agents_dir)
        prepared = await prepare_agent_run(
            self._agent,
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
        config.stream_subagent_deltas = subagent_deltas
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
            resume_all_projects=resume_all_projects,
            fork=fork,
            fork_at=fork_at,
            store=self._store,
            collection_model=prepared.collection,
            collection_policy=prepared.collection_policy,
            model_delta_consumer=on_delta,
        )

    async def _run_acp(self, task: str) -> AgentResult:
        """One ACP turn through the selected harness, wrapped as an AgentResult.

        The harness resolves through the shared registry (unknown and
        disabled ids are refused before anything launches) and the tenure
        attaches to a persisted unified session — selection is a session
        transition, not an in-memory choice.

        Approvals are fail-closed, as in a headless ``garuda run --runtime``
        (#801): every ``session/request_permission`` ask parks in the session
        approval broker and is denied at once (audited), so no ask is ever
        left unanswered. ``SoftwareAgent`` takes no approval callback; use
        ``Conversation(approval_handler=...)`` to answer asks. A run that ends
        by exception, cancellation or timeout settles its session ``failed``
        or ``cancelled``; it is never left ``running``.
        """
        from garuda.core.events import EventStore
        from garuda.core.sessions import SessionStore
        from garuda.interfaces.run_guard import broker_approval_handler
        from garuda.interfaces.runtime_cli import (
            acp_adapter_for_workspace,
            attach_acp_segment,
        )
        from garuda.runtime import RegistryError

        store = self._store or SessionStore()
        events = EventStore()
        # Bound to the resolved runtime id once the adapter exists; asks only
        # arrive during the prompt, after that.
        brokered: list = []

        async def _approve(action: str):
            return await brokered[0](action)

        try:
            _, adapter = acp_adapter_for_workspace(
                self.workspace,
                self.runtime_name,
                store=store,
                persist_dir=str(store.session_dir(events.session_id)),
                approval_handler=_approve,
            )
        except ValueError as exc:
            if "disabled by user configuration" in str(exc):
                raise RegistryError(str(exc)) from exc
            raise
        # No answerer: the broker denies every ask, audited (fail-closed).
        handler, _broker = broker_approval_handler(
            store, events.session_id, adapter.runtime_id, handler=None
        )
        brokered.append(handler)
        # The harness ran, not the SDK's native model or agent: record the
        # runtime the way the CLI does (#702).
        store.begin(
            events.session_id,
            task=task,
            model=f"acp:{adapter.runtime_id}",
            agent=adapter.runtime_id,
            workspace=self.workspace,
        )
        try:
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
                # The harness must run in a mode that brokers its approvals (#700).
                mode = await adapter.secure_mode()
                if mode is not None:
                    store.update_meta(events.session_id, {"acp_mode": mode})
                turn = await adapter.prompt(task)
            finally:
                await adapter.close()
                # Like the CLI (#690): the adapter's last usage report is
                # recorded whatever the outcome, not only after a completed turn.
                trail = await _acp_trail_with_usage(store, events.session_id, adapter)
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
        except BaseException as exc:
            _settle_unfinished(store, events.session_id, exc)
            raise
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
            agent=self._agent,
            agents_dir=self.agents_dir,
            mcp_config=self.mcp_config,
            mode=self.mode,
            workspace_kind=self.workspace_kind,
            docker_image=self.docker_image,
            docker_host=self.docker_host,
            runtime=self.runtime_name,
            store=self._store,
            extra_tools=self._extra_tools,
            load_project_tools=self._load_project_tools,
        )


async def _acp_trail_with_usage(store, session_id: str, adapter) -> list:
    """The turn's events, with its usage recorded; never masks the turn's outcome."""
    import logging

    try:
        trail, _ = await adapter.poll_events(0)
    except Exception:
        logging.getLogger(__name__).warning("acp events not read", exc_info=True)
        return []
    try:
        from garuda.observability.usage import record_turn_acp_usage

        record_turn_acp_usage(store, session_id, trail, runtime_id=adapter.runtime_id,
                              adapter_version=adapter.reported_adapter_version,
                              segment=adapter.native_session_id or session_id)
    except Exception:
        logging.getLogger(__name__).warning("acp usage not recorded", exc_info=True)
    return trail


def _settle_unfinished(store, session_id: str, exc: BaseException) -> None:
    """Record an ACP run that ended without a result; never masks ``exc``."""
    import asyncio
    import logging
    from datetime import datetime, timezone

    from garuda.runtime.session_state import interrupted

    cancelled = isinstance(exc, asyncio.CancelledError)
    try:
        store.update_meta(session_id, {
            "status": "cancelled" if cancelled else "failed",
            "state": interrupted(cancelled=cancelled), "verified": False,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        })
    except Exception:
        logging.getLogger(__name__).warning("Failed to record the ACP run's end", exc_info=True)
