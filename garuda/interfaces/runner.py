from pathlib import Path

from garuda.core.events import EventStore
from garuda.core.permissions import PermissionEngine
from garuda.plugins.hooks import HookRegistry
from garuda.types import AgentConfig, AgentResult
from garuda.workspace.docker import DockerWorkspace
from garuda.workspace.factory import create_workspace
from garuda.workspace.protocol import Environment
from garuda.workspace.remote import RemoteWorkspace
from garuda.workspace.tmux import TmuxEnvironment


async def resolve_environment(
    workspace_kind: str,
    workspace_root: str,
    docker_image: str,
    docker_host: str | None = None,
) -> tuple[Environment, object | None]:
    workspace = await create_workspace(
        workspace_kind,
        workspace_root,
        docker_image=docker_image,
        docker_host=docker_host,
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
) -> AgentResult:
    env, handle = await resolve_environment(
        workspace_kind, workspace, docker_image, docker_host=docker_host
    )
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
        )
    finally:
        await cleanup_workspace(handle)
        if mcp_manager is not None:
            await mcp_manager.close()
    if emit_json:
        for event in events.get_all():
            import json

            print(json.dumps(event))
    return result
