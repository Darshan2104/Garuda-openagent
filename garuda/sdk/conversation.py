"""Stateful conversation wrapper around Garuda agent runs."""

import asyncio
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
        runtime: str = "native",
        workspace_kind: str = "local",
        docker_image: str = "ubuntu:22.04",
        docker_host: str | None = None,
        store=None,
        approval_handler=None,
    ):
        self._workspace = str(workspace)
        self._model_name = model
        self._agent_name = agent
        self._agents_dir = Path(agents_dir) if agents_dir else None
        self._mcp_config = mcp_config
        self._mode = mode
        self._runtime = runtime
        self._workspace_kind = workspace_kind
        self._docker_image = docker_image
        self._docker_host = docker_host
        self._runtime_name = runtime
        self._store = store
        self._approval_handler = approval_handler
        self._session: AgentSession | None = None
        self._env: Environment | None = None
        self._env_handle: object | None = None
        self._acp: Any | None = None
        self._acp_trail: list = []
        self._sdk_session_id: str | None = None
        self._approval_task: Any | None = None
        self._approval_broker: Any | None = None

    async def _ensure_session(self) -> AgentSession:
        if self._session is None:
            # A conversation owns an environment for several turns, so enforce
            # trusted runtime selection before that environment is allocated.
            from garuda.agents.setup import prepare_runtime_catalog

            catalog = prepare_runtime_catalog(self._workspace)
            catalog.select_for_native_facade(self._runtime)
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

    def _record_baseline(self, store, session_id: str) -> None:
        """Capture the authoritative baseline once per SDK session (local
        workspaces only — other kinds do not share the host filesystem)."""
        if self._workspace_kind != "local":
            return
        try:
            from garuda.workspace.diff import capture_baseline

            store.record_baseline(
                session_id, capture_baseline(self._workspace).to_dict()
            )
        except Exception:
            pass

    async def _run_acp(self, task: str) -> AgentResult:
        """One turn on the held ACP runtime; the harness keeps the session.

        The harness resolves through the shared registry (unknown and
        disabled ids refused), and every tenure attaches to one persisted
        unified session. Human answers to agent approval requests flow
        through the optional approval handler.
        """
        from garuda.core.events import EventStore
        from garuda.core.sessions import SessionStore
        from garuda.interfaces.runtime_cli import (
            RUNTIME_API_VERSION,
            acp_adapter_for_workspace,
            attach_acp_segment,
        )

        if self._acp is None:
            store = self._store or SessionStore()
            self._store = store
            events = EventStore()
            store.begin(
                events.session_id,
                task=task,
                model=self._model_name,
                agent=self._agent_name,
                workspace=self._workspace,
            )
            _, adapter = acp_adapter_for_workspace(
                self._workspace,
                self._runtime_name,
                store=store,
                persist_dir=str(store.session_dir(events.session_id)),
            )
            await adapter.start(task=task, session_id=events.session_id)
            self._acp = adapter
            self._sdk_session_id = events.session_id
            self._record_baseline(store, events.session_id)
            attach_acp_segment(store, events.session_id, adapter)
            from garuda.acp.broker import ApprovalBroker
            from garuda.core.permissions import PermissionEngine

            self._approval_broker = ApprovalBroker(
                engine=PermissionEngine(), store=store
            )

            if self._approval_handler is not None:
                async def _answer(request) -> bool:
                    try:
                        return bool(await self._approval_handler(request.action))
                    except Exception:
                        return False

                self._approval_broker.set_answerer(_answer)
            else:
                self._approval_broker.set_answerer(lambda request: False)
            self._approval_task = self._launch_approval_responder()
        turn = await self._acp.prompt(task)
        events, _ = await self._acp.poll_events(len(self._acp_trail))
        self._acp_trail.extend(events)
        assert self._store is not None and self._sdk_session_id is not None
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
            metadata={
                "runtime": self._runtime_name,
                "api": RUNTIME_API_VERSION,
                "session_id": self._sdk_session_id,
            },
        )

    def _launch_approval_responder(self):
        """Relay agent approval requests to the human handler, if any."""
        if self._approval_broker is None or self._acp is None:
            return None

        adapter = self._acp
        broker = self._approval_broker
        seen: set[str] = set()

        async def _respond() -> None:
            try:
                while True:
                    await asyncio.sleep(0.1)
                    trail, _ = await adapter.poll_events(0)
                    for event in trail:
                        if event.kind.value != "approval_request":
                            continue
                        approval_id = event.payload.get("approval_id", "")
                        if not approval_id or approval_id in seen:
                            continue
                        seen.add(approval_id)
                        allow, _reason = await broker.decide_acp(
                            event.payload.get("family", "approval"),
                            event.payload.get("action", ""),
                            runtime_id=adapter.runtime_id,
                            session_id=self._sdk_session_id or "",
                            approval_id=approval_id,
                        )
                        try:
                            await adapter.permission_response(
                                approval_id=approval_id, allow=bool(allow)
                            )
                        except Exception:
                            pass
            except asyncio.CancelledError:
                raise

        return asyncio.ensure_future(_respond())

    async def switch_runtime(self, target: str) -> dict[str, Any]:
        """Move this conversation to another harness.

        ACP to ACP runs the real handoff transaction (pause → checkpoint →
        capture → start → acknowledge or rollback) and attaches the new
        tenure to the same unified session. From native (or a fresh
        conversation), the target starts directly and attaches — the
        in-process session is retained, never closed. Unknown and disabled
        targets are refused before anything moves.
        """
        from garuda.interfaces.runtime_cli import acp_adapter_for_workspace, attach_acp_segment
        from garuda.runtime.handoff import execute_handoff

        _, new_adapter = acp_adapter_for_workspace(self._workspace, target)
        if self._acp is None:
            await new_adapter.close()
            raise ValueError(
                "native-to-ACP switching is not available through Conversation; "
                "start an ACP conversation or use the transactional runtime handoff"
            )
        store = self._store
        assert store is not None and self._sdk_session_id is not None
        previous = self._runtime_name

        from garuda.context.pack import compile_handoff
        from garuda.context.state_card import WorkingState

        unified = store.load_unified(self._sdk_session_id)
        state = WorkingState(task=unified.legacy.get("task", self._sdk_session_id))
        package: list[str] = []

        def checkpoint() -> None:
            _doc, body = compile_handoff(
                state,
                source_runtime=unified.active.runtime_id,
                session_id=self._sdk_session_id,
                native_session_id=unified.active.native_session_id or "",
            )
            package[:] = [body]

        async def deliver(target_runtime) -> None:
            if not package:
                raise RuntimeError("handoff package was not generated")
            await target_runtime.prompt(package[0])
            await target_runtime.poll_events(0)

        def _factory():
            _, built = acp_adapter_for_workspace(
                self._workspace,
                target,
                store=store,
                persist_dir=str(store.session_dir(self._sdk_session_id)),
            )
            return built

        tx, started = await execute_handoff(
            session_id=self._sdk_session_id,
            source=self._acp,
            target_factory=_factory,
            store=store,
            checkpoint=checkpoint,
            deliver=deliver,
            workspace=self._workspace,
        )
        attach_acp_segment(store, self._sdk_session_id, started)
        await self._replace_adapter(started, target)
        return {
            "session_id": self._sdk_session_id,
            "target_runtime": target,
            "previous_runtime": previous,
            "phase": tx.phase.value,
        }

    async def _replace_adapter(self, adapter, runtime_name: str) -> None:
        if self._approval_task is not None:
            self._approval_task.cancel()
            try:
                await self._approval_task
            except (asyncio.CancelledError, Exception):
                pass
            self._approval_task = None
        self._acp = adapter
        self._acp_trail = []
        self._runtime_name = runtime_name
        self._approval_task = self._launch_approval_responder()

    async def close(self) -> None:
        if self._approval_task is not None:
            self._approval_task.cancel()
            try:
                await self._approval_task
            except (asyncio.CancelledError, Exception):
                pass
            self._approval_task = None
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
