import asyncio
import json
import logging
from datetime import datetime, timezone

from garuda.context.manager import ContextManager
from garuda.core.events import EventStore
from garuda.core.permissions import PermissionEngine
from garuda.core.run_state import reserved_output_tokens
from garuda.core.sessions import SessionStore
from garuda.plugins.hooks import HookRegistry, build_hook_registry
from garuda.runtime.native import NativeGarudaRuntime
from garuda.types import AgentConfig, AgentResult, Message, Role
from garuda.workspace.docker import DockerWorkspace
from garuda.workspace.factory import create_workspace
from garuda.workspace.protocol import Environment
from garuda.workspace.remote import RemoteWorkspace
from garuda.workspace.sandbox_policy import DockerLimits, SandboxPolicy
from garuda.workspace.tmux import TmuxEnvironment

logger = logging.getLogger(__name__)


def _sandbox_policy_from_config(config: AgentConfig | None) -> SandboxPolicy | None:
    if config is None:
        return None
    return SandboxPolicy(
        allow_network=config.sandbox_allow_network,
        require_sandbox=config.sandbox_require,
    )


def _docker_limits_from_config(config: AgentConfig | None) -> DockerLimits | None:
    if config is None:
        return None
    return DockerLimits(
        memory=config.docker_memory,
        cpus=config.docker_cpus,
        network=config.docker_network,
    )


async def resolve_environment(
    workspace_kind: str,
    workspace_root: str,
    docker_image: str,
    docker_host: str | None = None,
    config: AgentConfig | None = None,
) -> tuple[Environment, object | None]:
    workspace = await create_workspace(
        workspace_kind,
        workspace_root,
        docker_image=docker_image,
        docker_host=docker_host,
        sandbox_policy=_sandbox_policy_from_config(config),
        docker_limits=_docker_limits_from_config(config),
    )
    if isinstance(workspace, DockerWorkspace):
        return workspace.get_environment(), workspace
    if isinstance(workspace, RemoteWorkspace):
        return workspace.get_environment(), workspace
    return workspace, workspace


async def cleanup_workspace(handle: object | None) -> None:
    if handle is None:
        return
    if isinstance(handle, (DockerWorkspace, RemoteWorkspace)):
        await handle.stop()
    elif isinstance(handle, TmuxEnvironment):
        await handle.stop()


def update_session_meta(store: SessionStore, session_id: str, updates: dict) -> None:
    """Merge extra fields into a session's meta.json (SessionStore.begin has a
    fixed schema, so resume provenance and failure states are patched in here).

    Delegates to the store so this writer and ``finish()`` contend on the same
    lock; two independent read-modify-write cycles used to drop each other's
    fields. Still never raises — losing provenance must not fail a completed run.
    """
    try:
        store.update_meta(
            session_id, {**updates, "updated_at": datetime.now(timezone.utc).isoformat()}
        )
    except OSError:
        pass


def _with_current_system_prompt(messages: list[Message], system_prompt: str) -> list[Message]:
    """Replace the replayed history's leading system message with the current one.

    Only the first system message is touched; everything else — including the
    conversation's own structure and tool_call pairing — is preserved. When the
    history has no system message, one is prepended.
    """
    updated = list(messages)
    for index, message in enumerate(updated):
        if message.role == Role.SYSTEM:
            updated[index] = Message(role=Role.SYSTEM, content=system_prompt)
            return updated
    return [Message(role=Role.SYSTEM, content=system_prompt), *updated]


def build_resumed_context(
    store: SessionStore,
    resumed_session_id: str,
    task: str,
    model,
    config: AgentConfig,
) -> ContextManager:
    """Seed a ContextManager with a prior session's messages plus the new task."""
    messages = store.load_messages(resumed_session_id)
    # Replay the conversation but not its stale system prompt. The saved messages
    # carry the prompt from the original run, so skills added since, an edited
    # AGENTS.md, or a changed profile were all silently ignored on resume — the
    # session kept obeying instructions the workspace no longer has.
    if config.system_prompt:
        messages = _with_current_system_prompt(messages, config.system_prompt)
    context = ContextManager(
        model=model,
        max_output_bytes=config.max_output_bytes,
        proactive_threshold=config.proactive_summarize_threshold,
        max_context_tokens=config.max_context_tokens,
        enable_three_step_summary=config.enable_three_step_summary,
        task=task,
        reserved_output_tokens=reserved_output_tokens(config),
        safety_margin_tokens=config.context_safety_margin_tokens,
        adaptive_output=config.enable_adaptive_output,
        min_output_bytes=config.min_output_bytes,
    )
    context.seed(messages)
    context.append(Message(role=Role.USER, content=task))
    return context


async def run_agent_task(
    task: str,
    model,
    agent,
    tools,
    config: AgentConfig,
    permissions: PermissionEngine,
    workspace: str,
    events: EventStore,
    emit_json: bool = False,
    workspace_kind: str = "local",
    docker_image: str = "ubuntu:22.04",
    docker_host: str | None = None,
    hooks: HookRegistry | None = None,
    mcp_manager=None,
    agents_dir=None,
    context: ContextManager | None = None,
    close_mcp: bool = True,
    resume: str | None = None,
    store: SessionStore | None = None,
) -> AgentResult:
    # Callers that read sessions back from a specific root must be able to write to that
    # same root. The dashboard's `--sessions-dir` is the case: without this, a run it
    # launched would persist to the default location and be invisible in the list that
    # launched it — a silent divergence, not an error.
    store = store or SessionStore()

    # Every native run — CLI, SDK, server, web — executes through the
    # NativeGarudaRuntime boundary (P0.7). The bridge owns the unified session
    # record and the normalized event log; execution below is unchanged.
    runtime = NativeGarudaRuntime(
        agent=agent,
        model=model,
        tools=tools,
        config=config,
        permissions=permissions,
        store=store,
        events=events,
    )

    resumed_from: str | None = None
    initial_state: dict | None = None
    if resume:
        resumed_from = store.resolve(resume)
        if context is None:
            context = build_resumed_context(store, resumed_from, task, model, config)
        # Restore pack facts after restart: the persisted WorkingState is the
        # input the pack compiler renders from, so hydrating it here means the
        # re-synced current-task/handoff carry the pre-restart facts verbatim.
        try:
            initial_state = store.load_state(resumed_from) or None
        except Exception:
            logger.warning("Failed to load persisted working state", exc_info=True)
            initial_state = None

    # One mutating session owns a workspace (P0.16). Acquired before any
    # environment is resolved so a refused run burns nothing, held for the
    # whole facade call, and released last in `finally`. A live foreign
    # mutating lease — including a second concurrent `run_agent_task` on this
    # workspace — fails here instead of interleaving mutations. Fail-closed:
    # `LeaseConflictError`/`LeaseError` propagate, never degrade to unlocked.
    from garuda.workspace.lease import DEFAULT_TTL_SEC, LeaseStore

    lease_store = LeaseStore()
    lease_store.acquire(workspace, events.session_id, mode="mutating")
    lease_ttl = DEFAULT_TTL_SEC

    async def _lease_heartbeat() -> None:
        while True:
            await asyncio.sleep(lease_ttl / 3)
            # Losing the lease means this run can no longer prove exclusive
            # mutation authority. Propagate the error to the driver below;
            # continuing would let a stale takeover and this run mutate at
            # the same time.
            lease_store.heartbeat(workspace, events.session_id)

    heartbeat_task = asyncio.ensure_future(_lease_heartbeat())

    # One approval path for every run through this facade (P0.17): the engine's
    # ASK responder becomes the session broker, so native tool approvals and
    # ACP-side decisions share parking, timeout/disconnect denial, and the
    # persisted audit — CLI headless, SDK, and server jobs traverse it alike.
    # A caller-supplied interactive handler is not bypassed; it becomes the
    # broker's answerer. With no handler, asks deny immediately (audited)
    # instead of hanging until timeout.
    from garuda.acp.broker import ApprovalBroker

    approval_broker = ApprovalBroker(engine=permissions, store=store)
    previous_handler = permissions.approval_handler
    if previous_handler is not None:
        def _make_answerer(handler):
            async def _answer(request) -> bool:
                try:
                    return bool(await handler(request.action))
                except Exception:
                    logger.warning(
                        "Approval answerer failed; denying", exc_info=True
                    )
                    return False

            return _answer

        approval_broker.set_answerer(_make_answerer(previous_handler))
    else:
        async def _deny_all(request) -> bool:
            return False

        approval_broker.set_answerer(_deny_all)
    permissions.install_approval_handler(
        approval_broker.handler(session_id=events.session_id)
    )

    async def _abandon_lease() -> None:
        """Cancel the heartbeat and release, for startup paths that never
        reach the main `try/finally` below. Best-effort; never masks the
        original error."""
        heartbeat_task.cancel()
        try:
            await heartbeat_task
        except (asyncio.CancelledError, Exception):
            pass
        try:
            lease_store.release(workspace, events.session_id)
        except Exception:
            logger.warning("Lease release failed", exc_info=True)

    try:
        await runtime.start(task=task, session_id=events.session_id)
        events_path = store.events_path(events.session_id)
        events.attach_persistence(events_path)
        if resumed_from:
            update_session_meta(store, events.session_id, {"resumed_from": resumed_from})
            # Copy the prior session's tool-output buffers into the new session dir so
            # inherited [buffer:...] stubs in the resumed conversation still resolve
            # (buffers are keyed to the session id, which changes on resume).
            try:
                import shutil

                src = store.session_dir(resumed_from) / "buffers"
                dst = store.session_dir(events.session_id) / "buffers"
                if src.is_dir() and not dst.exists():
                    shutil.copytree(src, dst)
            except Exception:
                logger.warning("Failed to copy resumed session buffers", exc_info=True)

        if hooks is None:
            hooks = build_hook_registry(workspace)

        # Single-writer pack publisher for this workspace (P0.9). Best-effort:
        # when the workspace root is not a writable local dir the manager simply
        # never syncs, and every sync failure is caught inside RunState.
        pack_manager = None
        pack_git_evidence = ""
        try:
            from pathlib import Path as _Path

            from garuda.context.pack import ContextPackManager, collect_git_evidence

            pack_root = _Path(workspace) / ".context" if workspace else None
            if pack_root is not None:
                pack_manager = ContextPackManager(pack_root)
                pack_git_evidence = collect_git_evidence(workspace)
        except Exception:
            logger.debug("Context pack manager unavailable for workspace", exc_info=True)
            pack_manager = None
            pack_git_evidence = ""

        env, handle = await resolve_environment(
            workspace_kind, workspace, docker_image, docker_host=docker_host, config=config
        )
        result: AgentResult | None = None
        await hooks.on_session_start(task=task, session_id=events.session_id)

        async def _driver(*, task: str, turn: int, trail: EventStore):
            return await agent.run(
                task=task,
                model=model,
                env=env,
                tools=tools,
                config=config,
                events=trail,
                permissions=permissions,
                hooks=hooks,
                agents_dir=agents_dir,
                context=context,
                checkpoint=lambda msgs: store.checkpoint_messages(events.session_id, msgs),
                state_checkpoint=lambda state: store.checkpoint_state(events.session_id, state),
                pack_manager=pack_manager,
                pack_source_runtime="native",
                pack_git_evidence=pack_git_evidence,
                initial_state=initial_state,
            )

        runtime.install_driver(_driver)
    except Exception:
        await _abandon_lease()
        raise
    try:
        prompt_task = asyncio.ensure_future(runtime.prompt(task))
        done, _ = await asyncio.wait(
            {prompt_task, heartbeat_task}, return_when=asyncio.FIRST_COMPLETED
        )
        if heartbeat_task in done:
            heartbeat_error = heartbeat_task.exception()
            if not prompt_task.done():
                prompt_task.cancel()
                try:
                    await prompt_task
                except (asyncio.CancelledError, Exception):
                    pass
            if heartbeat_error is None:
                from garuda.workspace.lease import LeaseError

                raise LeaseError("lease heartbeat stopped unexpectedly")
            raise heartbeat_error
        await prompt_task
        result = runtime.last_result
    except Exception:
        result = None
        raise
    finally:
        try:
            # Kill any background tasks this session left running before tearing down
            # the workspace (essential for the local env, where nothing else reaps them).
            try:
                from garuda.tools.background import reap_session

                await reap_session(events.session_id, env)
            except Exception:
                pass
            # Close a persistent shell if the local env opened one.
            if hasattr(env, "aclose"):
                try:
                    await env.aclose()
                except Exception:
                    logger.warning("Failed to close persistent shell", exc_info=True)
            # Each teardown step is guarded individually: a failure to stop a container
            # or close an MCP server must not skip the two things that follow, or the
            # session stays marked "running" in the index forever and no session-end
            # hook ever fires — the state you most need after a crash.
            try:
                await cleanup_workspace(handle)
            except Exception:
                logger.warning("Workspace cleanup failed", exc_info=True)
            if close_mcp and mcp_manager is not None:
                try:
                    await mcp_manager.close()
                except Exception:
                    logger.warning("MCP manager close failed", exc_info=True)
            if result is not None:
                store.finish(events.session_id, result)
                summary = {
                    "session_id": events.session_id,
                    "success": result.success,
                    "turns": result.turns,
                    "final_message": result.final_message[:2000],
                }
            else:
                update_session_meta(store, events.session_id, {"status": "failed"})
                summary = {"session_id": events.session_id, "success": False, "turns": 0}
            try:
                await runtime.close()
            except Exception:
                logger.warning("Runtime close failed", exc_info=True)
            await hooks.on_session_end(summary)
        finally:
            # Release the mutating lease last even if session persistence or a
            # lifecycle hook fails. Otherwise the heartbeat task can outlive
            # this call and hold the workspace indefinitely.
            await _abandon_lease()
    if emit_json:
        for event in events.get_all():
            print(json.dumps(event))
    return result
