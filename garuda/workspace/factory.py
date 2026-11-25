from pathlib import Path

from garuda.workspace.docker import DockerWorkspace
from garuda.workspace.local import LocalEnvironment
from garuda.workspace.protocol import Environment
from garuda.workspace.remote import RemoteWorkspace
from garuda.workspace.sandbox import SandboxEnvironment
from garuda.workspace.sandbox_policy import DockerLimits, SandboxPolicy
from garuda.workspace.tmux import TmuxEnvironment

#: Every workspace kind `create_workspace` accepts. Entry-point `choices` and
#: the workspace-evidence classification (`garuda.workspace.evidence`) are
#: tested against this set, so a new kind cannot ship unclassified.
WORKSPACE_KINDS = ("local", "sandbox", "tmux", "docker", "remote")


class WorkspaceStartError(RuntimeError):
    """A container or tmux workspace could not be started (#671).

    A ``RuntimeError`` like the start failures it wraps, so existing handlers
    still catch it; its message is one bounded line led by the stable code
    ``workspace.start_failed``, so entry points can print it as a refusal
    instead of a traceback.
    """

    code = "workspace.start_failed"

    def __init__(self, kind: str, target: str, detail: str):
        lines = [line.strip() for line in detail.splitlines() if line.strip()]
        # Docker appends a usage hint that says nothing about the failure.
        lines = [line for line in lines if not line.startswith("Run 'docker")]
        reason = " ".join(lines)[:400] or "no detail reported"
        super().__init__(f"{self.code}: {kind} workspace could not start {target}: {reason}")
        self.kind = kind


async def _start(workspace, kind: str, target: str):
    try:
        await workspace.start()
    except Exception as exc:
        raise WorkspaceStartError(kind, target, str(exc) or type(exc).__name__) from exc
    return workspace


def create_environment(
    kind: str,
    workspace_root: str | Path,
    docker_image: str = "ubuntu:22.04",
    docker_host: str | None = None,
    sandbox_policy: SandboxPolicy | None = None,
) -> Environment:
    root = Path(workspace_root).resolve()
    if kind == "local":
        return LocalEnvironment(workspace_root=root)
    if kind == "sandbox":
        return SandboxEnvironment(workspace_root=root, policy=sandbox_policy)
    if kind == "tmux":
        return TmuxEnvironment(workspace_root=root)
    if kind == "docker":
        raise RuntimeError("Use create_workspace('docker') and workspace.get_environment()")
    if kind == "remote":
        raise RuntimeError("Use create_workspace('remote') and workspace.get_environment()")
    raise ValueError(f"Unknown workspace kind: {kind}")


async def create_workspace(
    kind: str,
    workspace_root: str | Path,
    docker_image: str = "ubuntu:22.04",
    docker_host: str | None = None,
    sandbox_policy: SandboxPolicy | None = None,
    docker_limits: DockerLimits | None = None,
) -> LocalEnvironment | SandboxEnvironment | TmuxEnvironment | DockerWorkspace | RemoteWorkspace:
    root = Path(workspace_root).resolve()
    if kind == "local":
        return LocalEnvironment(workspace_root=root)
    if kind == "sandbox":
        return SandboxEnvironment(workspace_root=root, policy=sandbox_policy)
    if kind == "tmux":
        return await _start(TmuxEnvironment(workspace_root=root), kind, "a tmux session")
    if kind == "docker":
        workspace = DockerWorkspace(workspace_root=root, image=docker_image, limits=docker_limits)
        return await _start(workspace, kind, f"image {docker_image!r}")
    if kind == "remote":
        workspace = RemoteWorkspace(
            workspace_root=root, image=docker_image, docker_host=docker_host, limits=docker_limits
        )
        host = docker_host or "the DOCKER_HOST daemon"
        return await _start(workspace, kind, f"image {docker_image!r} on {host}")
    raise ValueError(f"Unknown workspace kind: {kind}")
