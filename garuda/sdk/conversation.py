"""Stateful conversation wrapper around Garuda agent runs."""

import asyncio
import logging
from contextvars import ContextVar, copy_context
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from garuda.core.events import EventStore
from garuda.interfaces.runner import resolve_environment
from garuda.interfaces.session import AgentSession
from garuda.model.protocol import DEFAULT_MODEL
from garuda.types import AgentResult
from garuda.workspace.protocol import Environment
from garuda.workspace.settlement import settle_task

if TYPE_CHECKING:
    from garuda.model.streaming import DeltaConsumer
    from garuda.tools.protocol import Tool


logger = logging.getLogger(__name__)

_native_turn_keys: ContextVar[frozenset[object]] = ContextVar("_native_turn_keys", default=frozenset())


class Conversation:
    """Multi-turn Garuda session with shared event history and LLM context."""

    def __init__(
        self,
        workspace: str | Path = ".",
        model: str | object = DEFAULT_MODEL,
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
        reasoning_model: str | object | None = None,
        collection_model: str | object | None = None,
        no_collection: bool = False,
        model_binding: str | None = None,
        extra_tools: list["Tool"] | None = None,
        load_project_tools: bool | None = None,
    ):
        self._workspace = str(workspace)
        self._model_name = model
        self._agent_name = agent  # a name or a frozen AgentSpec
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
        self._reasoning_model = reasoning_model
        self._collection_model = collection_model
        self._no_collection = no_collection
        self._model_binding = model_binding
        self._extra_tools = list(extra_tools or [])
        self._load_project_tools = load_project_tools
        self._session: AgentSession | None = None
        self._env: Environment | None = None
        self._acp: Any | None = None
        self._acp_trail: list = []
        self._acp_turn: asyncio.Task[AgentResult] | None = None
        self._acp_close: asyncio.Task[None] | None = None
        self._acp_result: AgentResult | None = None
        self._acp_cancelled = False
        self._acp_turn_count = 0
        self._sdk_session_id: str | None = None
        self._approval_task: Any | None = None
        self._approval_broker: Any | None = None
        self._lease: Any | None = None
        self._live: Any | None = None
        self._record: Any | None = None
        self._native_turn: asyncio.Task[AgentResult] | None = None
        self._active_native_turn_key: object | None = None
        self._native_close: asyncio.Task[None] | None = None

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
                reasoning_model=self._reasoning_model,
                collection_model=self._collection_model,
                no_collection=self._no_collection,
                model_binding=self._model_binding,
                extra_tools=self._extra_tools,
                load_project_tools=self._load_project_tools,
                workspace=self._workspace,
                agents_dir=self._agents_dir,
                mcp_config_path=self._mcp_config,
                mode=self._mode,
                workspace_kind=self._workspace_kind,
                docker_image=self._docker_image,
                docker_host=self._docker_host,
                approval_handler=self._approval_handler,
            )
        return self._session

    async def run(
        self, task: str, *, on_delta: "DeltaConsumer | None" = None,
        subagent_deltas: bool = False,
    ) -> AgentResult:
        """Run a task in this conversation.

        ``on_delta`` (native runtime only) receives the live ``LiveDelta``
        fragments the loop forwards while each model call streams; sync and async
        callables are accepted. The result still comes from the assembled model
        response, and a consumer that raises is detached for that call (recorded
        as ``live_consumer_error``) without changing the turn's outcome.
        ``subagent_deltas=True`` requires ``on_delta`` and opts into child
        deltas labelled with ``source`` (session id), ``agent`` and ``depth``.
        Group by source: concurrent workers interleave by scheduling, preserving
        order within each source. A labelled ``discard`` clears only that source.
        Consult and collection children are excluded; the default is parent only.
        """
        if subagent_deltas and on_delta is None:
            raise ValueError("subagent_deltas requires on_delta")
        if on_delta is not None and self._runtime_name != "native":
            raise ValueError("live deltas are only available for the native runtime")
        from garuda.sdk.errors import require_local_workspace

        require_local_workspace(self._workspace, self._workspace_kind)
        if self._runtime_name != "native":
            from garuda.sdk.errors import refuse_unservable_options, refuse_unservable_tools

            refuse_unservable_tools(self._runtime_name, self._extra_tools, self._load_project_tools)
            refuse_unservable_options(
                self._runtime_name, model=self._model_name, agent=self._agent_name,
                reasoning_model=self._reasoning_model, collection_model=self._collection_model,
                model_binding=self._model_binding, mode=self._mode,
                mcp_config=self._mcp_config, workspace_kind=self._workspace_kind,
                docker_image=self._docker_image, docker_host=self._docker_host)
            return await self._run_acp(task)
        if self._native_close is not None and not self._native_close.done():
            raise RuntimeError("The native conversation is closing")
        if self._native_turn is not None and not self._native_turn.done():
            raise RuntimeError("The native conversation already has an active turn")
        self._native_close = None
        key = self._active_native_turn_key = object()
        owner_context = copy_context()
        owner_context.run(_native_turn_keys.set, _native_turn_keys.get() | {key})
        turn = self._native_turn = asyncio.create_task(
            self._run_native(task, on_delta=on_delta, subagent_deltas=subagent_deltas),
            context=owner_context,
        )
        try:
            return await asyncio.shield(turn)
        except asyncio.CancelledError as cancelled:
            if not turn.done() and not turn.cancelling():
                turn.cancel()
            try:
                await settle_task(turn)
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                raise cancelled from exc
            raise
        finally:
            if self._native_turn is turn:
                self._native_turn = None
                self._active_native_turn_key = None

    async def _run_native(
        self, task: str, *, on_delta: "DeltaConsumer | None", subagent_deltas: bool,
    ) -> AgentResult:
        session = await self._ensure_session()
        session.config.stream_subagent_deltas = subagent_deltas
        live = await self._ensure_live(session, task)
        context = self._record.begin_turn(task=task, session=session)
        return await live.turn(self._record.turn(session.agent.run(
            task=task,
            model=session.model,
            env=live.env,
            tools=session.tools,
            config=session.config,
            events=session.events,
            permissions=session.permissions,
            agents_dir=session.agents_dir,
            context=context,
            workspace_delta_loader=live.delta_loader,
            collection_model=getattr(session, "collection", None),
            collection_policy=getattr(session, "collection_policy", None),
            model_delta_consumer=on_delta,
            **self._record.checkpoints,
        )))

    async def _ensure_live(self, session: AgentSession, task: str):
        """Open the conversation's session once, through the shared lifecycle (B.6).

        Capacity and the workspace lease (held for the conversation's whole
        life; another editor raises ``LeaseConflictError``), then the session
        record, the baseline, the approval broker and the environment.
        """
        if self._live is not None:
            return self._live
        from garuda.core.sessions import SessionStore
        from garuda.interfaces import session_service
        from garuda.interfaces.run_guard import lease_mode_for

        store = self._store or SessionStore()
        self._store = store
        recorded: dict[str, Any] = {}

        def begin(path: str) -> None:
            recorded["events_path"] = store.begin(
                session.events.session_id,
                task=task,
                model=getattr(session.model, "model_name", str(self._model_name)),
                agent=session.profile.name,
                workspace=path,
            )

        try:
            live = await session_service.open_session(
                store,
                session.events.session_id,
                self._workspace,
                workspace_kind=self._workspace_kind,
                permissions=session.permissions,
                lease_mode=lease_mode_for(getattr(session.config, "permission_mode", None)),
                docker_image=self._docker_image,
                docker_host=self._docker_host,
                begin=begin,
                resolve_env=resolve_environment,
            )
        except session_service.SessionRefused as exc:
            raise exc.cause from exc
        session.events.attach_persistence(recorded["events_path"])
        from garuda.observability import usage as usage_ledger

        usage_ledger.attach(session.events, store)
        self._record = session_service.ConversationRecord(store, session.events.session_id, session.events)
        self._live, self._lease, self._env = live, live.lease, live.env
        return live

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
        from garuda.runtime import RuntimeClosedError

        if self._acp_close is not None and (not self._acp_close.done() or self._sdk_session_id is not None):
            raise RuntimeError("The ACP conversation is closing")
        if self._acp_turn is not None and not self._acp_turn.done():
            raise RuntimeError("The ACP conversation already has an active turn")
        self._acp_close = None
        self._acp_result = None
        # A cancelled turn closes the runtime (#813); a run refused for that keeps
        # the record's outcome `cancelled` rather than turning it into `failed`.
        refused_after_cancel = self._acp_cancelled
        self._acp_cancelled = False
        turn = self._acp_turn = asyncio.create_task(self._prompt_acp(task))
        try:
            return await asyncio.shield(turn)
        except RuntimeClosedError:
            self._acp_cancelled = self._acp_cancelled or refused_after_cancel
            raise
        except asyncio.CancelledError as cancelled:
            if not turn.done() and not turn.cancelling():
                self._acp_cancelled = True
                turn.cancel()
            try:
                await settle_task(turn)
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                raise cancelled from exc
            raise
        finally:
            if self._acp_turn is turn:
                self._acp_turn = None

    async def _acp_turn_events(self) -> list:
        """The turn's new events, with the adapter's usage report recorded."""
        events, _ = await self._acp.poll_events(len(self._acp_trail))
        self._acp_trail.extend(events)
        assert self._store is not None and self._sdk_session_id is not None
        from garuda.observability.usage import record_turn_acp_usage

        record_turn_acp_usage(self._store, self._sdk_session_id, events,
                              runtime_id=self._acp.runtime_id,
                              adapter_version=getattr(self._acp, "reported_adapter_version", "unknown"),
                              segment=self._acp.native_session_id or self._sdk_session_id)
        return events

    async def _prompt_acp(self, task: str) -> AgentResult:
        """One turn on the held ACP runtime; the harness keeps the session.

        The harness resolves through the shared registry (unknown and
        disabled ids refused), and every tenure attaches to one persisted
        unified session. Human answers to agent approval requests flow
        through the optional approval handler.
        """
        from garuda.acp.protocol import AcpCancelledError
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
            _, adapter = acp_adapter_for_workspace(
                self._workspace,
                self._runtime_name,
                store=store,
                persist_dir=str(store.session_dir(events.session_id)),
            )
            # The harness ran, not the SDK's native model or agent: record the
            # runtime the way the CLI does (#702).
            store.begin(
                events.session_id,
                task=task,
                model=f"acp:{adapter.runtime_id}",
                agent=adapter.runtime_id,
                workspace=self._workspace,
            )
            self._acp = adapter
            self._sdk_session_id = events.session_id
            try:
                store.ensure_unified(events.session_id)
                await adapter.start(task=task, session_id=events.session_id)
                # The harness must run in a mode that sends approvals here (#700).
                mode = await adapter.secure_mode()
                if mode is not None:
                    store.update_meta(events.session_id, {"acp_mode": mode})
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
            except BaseException as exc:
                # A failed or cancelled start must not leave a half-built
                # adapter held: close it and settle this session's record so
                # the next turn starts a fresh runtime.
                await self._abandon_acp_start(cancelled=isinstance(exc, asyncio.CancelledError))
                raise
        self._acp_turn_count += 1
        try:
            turn = await self._acp.prompt(task)
        except AcpCancelledError:
            self._acp_cancelled = True
            raise
        except Exception:
            # Like the CLI (#690): a failed turn keeps the cost its adapter
            # reported; recording it never masks the turn's error.
            try:
                await self._acp_turn_events()
            except Exception:
                logging.getLogger(__name__).warning("acp usage not recorded", exc_info=True)
            raise
        events = await self._acp_turn_events()
        texts = [
            e.payload.get("chunk", "") or e.payload.get("text", "")
            for e in events
            if e.kind.value == "message"
        ]
        result = AgentResult(
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
        self._acp_result = result
        return result

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
        from garuda.interfaces.runtime_cli import acp_adapter_for_workspace
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
        _, cursor = await started.poll_events(0)
        store.advance_event_cursor(self._sdk_session_id, cursor)
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
        if self._runtime_name == "native":
            turn = self._native_turn
            if turn is not None and not turn.done() and self._active_native_turn_key in _native_turn_keys.get():
                raise RuntimeError("Close the native conversation from outside its active turn")
            if self._native_close is None:
                self._native_close = asyncio.create_task(self._close_native())
            closing = self._native_close
        else:
            if asyncio.current_task() is self._approval_task:
                raise RuntimeError("Close the ACP conversation from outside its approval handler")
            if self._acp_close is None or (self._acp_close.done() and self._sdk_session_id is not None):
                self._acp_close = asyncio.create_task(self._settle_acp())
            closing = self._acp_close
        try:
            await asyncio.shield(closing)
        except asyncio.CancelledError as cancelled:
            try:
                await settle_task(closing)
            except Exception as exc:
                raise cancelled from exc
            raise

    async def _settle_acp(self) -> None:
        turn = self._acp_turn
        if turn is not None and not turn.done():
            self._acp_cancelled = True
            if not turn.cancelling():
                turn.cancel()
            await asyncio.gather(turn, return_exceptions=True)
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
        self._finish_acp_record()

    async def _abandon_acp_start(self, *, cancelled: bool) -> None:
        if cancelled:
            self._acp_cancelled = True
        adapter, self._acp = self._acp, None
        self._approval_broker = None
        try:
            if adapter is not None:
                await self._reap_failed_start(adapter)
        finally:
            self._acp_trail = []
            self._finish_acp_record()

    @staticmethod
    async def _reap_failed_start(adapter) -> None:
        """Close a half-started adapter through cancellation and close failure.

        The caller re-raises the original start error, so a close failure is
        logged rather than raised, and a cancellation (e.g. a concurrent
        ``close()``) waits for the harness to be reaped instead of abandoning it.
        """
        closing = asyncio.create_task(adapter.close())
        try:
            await asyncio.shield(closing)
        except asyncio.CancelledError:
            try:
                await settle_task(closing)
            except Exception:
                logger.warning("Closing the ACP runtime after a failed start failed", exc_info=True)
        except Exception:
            logger.warning("Closing the ACP runtime after a failed start failed", exc_info=True)

    def _finish_acp_record(self) -> None:
        from garuda.runtime.session_state import finished, interrupted

        if self._sdk_session_id is None:
            return
        result = self._acp_result
        state = (finished(success=result.success) if result is not None
                 else interrupted(cancelled=self._acp_cancelled))
        # The legacy status matches SessionStore.finish and the CLI/bg writers.
        status = "success" if state["outcome"] == "completed" else state["outcome"]
        self._store.update_meta(self._sdk_session_id, {
            "status": status, "state": state, "turns": self._acp_turn_count,
            "verified": False, "final_message": result.final_message[:2000] if result else "",
            "updated_at": datetime.now(timezone.utc).isoformat(),
        })
        self._sdk_session_id = None
        self._acp_result = None
        self._acp_cancelled = False
        self._acp_turn_count = 0

    async def _close_native(self) -> None:
        turn = self._native_turn
        if turn is not None:
            if not turn.done() and not turn.cancelling():
                turn.cancel()
            await asyncio.gather(turn, return_exceptions=True)
        session, self._session = self._session, None
        live, self._live, self._lease, self._env = self._live, None, None, None
        try:
            if session is not None:
                await session.close()
        finally:
            if live is not None:
                # Persist the settled outcome and release the workspace lease last.
                await live.close(lambda: self._finish_native(live.workspace))

    async def _finish_native(self, workspace: str) -> None:
        await self._record.finish(workspace)
        self._record = None

    @property
    def events(self) -> EventStore:
        if self._session is None:
            return EventStore()
        return self._session.events

    def trail(self) -> list:
        """Normalized runtime events so far (ACP conversations)."""
        return list(self._acp_trail)
