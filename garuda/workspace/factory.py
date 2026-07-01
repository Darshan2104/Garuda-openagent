from pathlib import Path

from garuda.workspace.docker import DockerEnvironment, DockerWorkspace
from garuda.workspace.local import LocalEnvironment
from garuda.workspace.protocol import Environment
from garuda.workspace.tmux import TmuxEnvironment


def create_environment(
    kind: str,
    workspace_root: str | Path,
    docker_image: str = "ubuntu:22.04",
) -> Environment:
    root = Path(workspace_root).resolve()
    if kind == "local":
        return LocalEnvironment(workspace_root=root)
    if kind == "tmux":
        return TmuxEnvironment(workspace_root=root)
    if kind == "docker":
        raise RuntimeError("Use create_workspace('docker') and workspace.get_environment()")
    raise ValueError(f"Unknown workspace kind: {kind}")


async def create_workspace(
    kind: str,
    workspace_root: str | Path,
    docker_image: str = "ubuntu:22.04",
) -> LocalEnvironment | TmuxEnvironment | DockerWorkspace:
    root = Path(workspace_root).resolve()
    if kind == "local":
        return LocalEnvironment(workspace_root=root)
    if kind == "tmux":
        env = TmuxEnvironment(workspace_root=root)
        await env.start()
        return env
    if kind == "docker":
        workspace = DockerWorkspace(workspace_root=root, image=docker_image)
        await workspace.start()
        return workspace
    raise ValueError(f"Unknown workspace kind: {kind}")
