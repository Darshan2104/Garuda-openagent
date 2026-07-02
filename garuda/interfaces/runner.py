import json
from datetime import datetime, timezone
from pathlib import Path

from garuda.context.manager import ContextManager
from garuda.core.events import EventStore
from garuda.core.permissions import PermissionEngine
from garuda.core.sessions import SessionStore
from garuda.plugins.hooks import HookRegistry, build_hook_registry
from garuda.types import AgentConfig, AgentResult, Message, Role
from garuda.workspace.docker import DockerWorkspace
from garuda.workspace.factory import create_workspace
from garuda.workspace.protocol import Environment
from garuda.workspace.remote import RemoteWorkspace
from garuda.workspace.sandbox_policy import DockerLimits, SandboxPolicy
from garuda.workspace.tmux import TmuxEnvironment


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
    fixed schema, so resume provenance and failure states are patched in here)."""
    meta_path = store.session_dir(session_id) / "meta.json"
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
        meta.update(updates)
        meta["updated_at"] = datetime.now(timezone.utc).isoformat()
        meta_path.write_text(json.dumps(meta, indent=2, default=str), encoding="utf-8")
    except OSError:
        pass


def build_resumed_context(
    store: SessionStore,
    resumed_session_id: str,
    task: str,
    model,
    config: AgentConfig,
) -> ContextManager:
    """Seed a ContextManager with a prior session's messages plus the new task."""
    messages = store.load_messages(resumed_session_id)
    context = ContextManager(
        model=model,
        max_output_bytes=config.max_output_bytes,
        proactive_threshold=config.proactive_summarize_threshold,
        max_context_tokens=config.max_context_tokens,
        enable_three_step_summary=config.enable_three_step_summary,
        task=task,
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
) -> AgentResult:
    store = SessionStore()

    resumed_from: str | None = None
    if resume:
        resumed_from = store.resolve(resume)
        if context is None:
            context = build_resumed_context(store, resumed_from, task, model, config)

    events_path = store.begin(
        session_id=events.session_id,
        task=task,
        model=getattr(model, "model_name", str(model)),
        agent=getattr(agent, "profile_name", "agent"),
        workspace=str(workspace),
    )
    events.attach_persistence(events_path)
    if resumed_from:
        update_session_meta(store, events.session_id, {"resumed_from": resumed_from})

    if hooks is None:
        hooks = build_hook_registry(workspace)

    env, handle = await resolve_environment(
        workspace_kind, workspace, docker_image, docker_host=docker_host, config=config
    )
    result: AgentResult | None = None
    await hooks.on_session_start(task=task, session_id=events.session_id)
    try:
        result = await agent.run(
            task=task,
            model=model,
            env=env,
            tools=tools,
            config=config,
            events=events,
            permissions=permissions,
            hooks=hooks,
            agents_dir=agents_dir,
            context=context,
        )
    finally:
        await cleanup_workspace(handle)
        if close_mcp and mcp_manager is not None:
            await mcp_manager.close()
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
        await hooks.on_session_end(summary)
    if emit_json:
        for event in events.get_all():
            print(json.dumps(event))
    return result
