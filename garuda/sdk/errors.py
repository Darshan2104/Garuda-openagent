"""Typed SDK refusals raised before anything launches."""

from __future__ import annotations

from garuda.agents.resolve import AgentNotFound
from garuda.runtime.registry import UnknownRuntime
from garuda.workspace.paths import (
    WorkspaceMissing,
    WorkspaceNotDirectory,
    WorkspaceUnavailable,
    require_workspace,
)

__all__ = ["AgentNotFound", "OptionsUnsupportedOnRuntime", "ToolsUnsupportedOnRuntime",
           "UnknownRuntime", "WorkspaceMissing", "WorkspaceNotDirectory", "WorkspaceUnavailable",
           "refuse_unservable_options", "refuse_unservable_tools", "require_local_workspace"]


def require_local_workspace(workspace, workspace_kind: str) -> None:
    """Refuse a local workspace that is not an existing directory, as the CLI does
    (``workspace.missing`` / ``workspace.not_directory``), before anything runs (#798)."""
    if workspace_kind == "local":
        require_workspace(workspace)


class ToolsUnsupportedOnRuntime(ValueError):
    """Custom SDK tools cannot reach the selected runtime (#702).

    An ACP harness runs its own tool loop and is never offered Garuda's
    in-process tools, so a registered tool would be dropped while the run
    still reported success. The run is refused before the harness starts.
    """

    code = "sdk.tools_unsupported"


def refuse_unservable_tools(runtime: str, extra_tools, load_project_tools) -> None:
    """Refuse custom or project tools on a runtime that cannot receive them."""
    if runtime == "native":
        return
    if extra_tools:
        names = ", ".join(sorted(getattr(t, "name", repr(t)) for t in extra_tools))
        raise ToolsUnsupportedOnRuntime(
            f"custom tools ({names}) cannot be offered to the ACP runtime {runtime!r}: "
            "the harness uses its own tools. Use runtime='native' for custom tools, "
            "or run without them")
    if load_project_tools:
        raise ToolsUnsupportedOnRuntime(
            f"load_project_tools=True cannot be honoured on the ACP runtime {runtime!r}: "
            "the harness uses its own tools. Use runtime='native', or leave it unset")


class OptionsUnsupportedOnRuntime(ValueError):
    """An SDK option cannot reach the selected runtime (#712, #724).

    An ACP harness runs with its own configured model, agent and MCP servers,
    and Garuda's run modes are native loop postures, so a ``model=``,
    ``reasoning_model=``, ``collection_model=``, ``model_binding=``,
    non-default ``agent=``, ``mcp_config=`` or non-default ``mode=`` would be
    ignored while the run still reported success. So would ``run(resume=...)``,
    ``resume_all_projects=`` and ``events=`` (#804): the SDK starts each ACP run
    as a fresh harness session. The run is refused before the harness starts.
    """

    code = "sdk.options_unsupported"


#: Run modes that ask for nothing beyond the default posture. Every other mode
#: (``eval``, ``rigorous``: native completion gates; ``readonly``: a narrower
#: permission than the SDK harness path enforces) is refused on ACP (#724).
DEFAULT_RUN_MODES = (None, "standard", "interactive")
#: The SDK's default ``docker_image``: unset unless a caller changes it.
DEFAULT_DOCKER_IMAGE = "ubuntu:22.04"


def refuse_unservable_options(runtime: str, *, model, agent, reasoning_model=None,
                              collection_model=None, model_binding=None, mode=None,
                              mcp_config=None, resume=None, resume_all_projects=False,
                              events=None, workspace_kind="local", docker_image=None,
                              docker_host=None) -> None:
    """Refuse native-only options on a runtime that would ignore them."""
    if runtime == "native":
        return
    from garuda.model.protocol import DEFAULT_MODEL

    ignored = []
    if model is not None and not (isinstance(model, str) and model == DEFAULT_MODEL):
        ignored.append("model")
    for name, value in (("reasoning_model", reasoning_model),
                        ("collection_model", collection_model),
                        ("model_binding", model_binding)):
        if value is not None:
            ignored.append(name)
    if not (isinstance(agent, str) and agent == "build"):
        ignored.append("agent")
    if ignored:
        raise OptionsUnsupportedOnRuntime(
            f"{', '.join(ignored)} cannot be applied on the ACP runtime {runtime!r}: the harness "
            "runs its own configured model and agent. Configure the harness instead, use "
            "runtime='native', or leave them unset")
    if mcp_config is not None:
        raise OptionsUnsupportedOnRuntime(
            f"mcp_config cannot be applied on the ACP runtime {runtime!r}: Garuda does not "
            "forward MCP servers to an external harness. Configure them in the harness, use "
            "runtime='native', or leave it unset")
    if mode not in DEFAULT_RUN_MODES:
        # The SDK harness path always secures the default permission's mode
        # (#700/#709); a readonly request would silently run wider, and
        # eval/rigorous gates exist only in the native loop. Refuse rather
        # than widen or drop it.
        raise OptionsUnsupportedOnRuntime(
            f"mode={mode!r} cannot be applied on the ACP runtime {runtime!r}: the SDK runs the "
            "harness in the mode its default permission allows, and run-mode gates exist only "
            "in the native loop. Use runtime='native', the CLI (`garuda run --runtime "
            f"{runtime} --mode readonly` runs a confined readonly harness), or leave it unset")
    environment = [name for name, unset in (
        ("workspace_kind", workspace_kind == "local"),
        ("docker_image", docker_image in (None, DEFAULT_DOCKER_IMAGE)),
        ("docker_host", docker_host is None)) if not unset]
    if environment:
        # The harness is launched on the host against the local workspace; a
        # Docker or remote environment would be dropped while the run reported
        # success. Refuse rather than run it somewhere the caller did not ask.
        raise OptionsUnsupportedOnRuntime(
            f"{', '.join(environment)} cannot be applied on the ACP runtime {runtime!r}: the "
            "SDK runs the harness on this host in the local workspace, not in a Docker or "
            "remote environment. Use runtime='native', the CLI (`garuda run --runtime "
            f"{runtime} --mode readonly` runs a confined readonly harness in Docker), or leave "
            "them unset")
    run_options = [name for name, value in (("resume", resume),
                                            ("resume_all_projects", resume_all_projects),
                                            ("events", events)) if value not in (None, False)]
    if run_options:
        # Each SDK ACP run is a fresh harness session with its own event store
        # (#804); a resume would silently start over. Refuse rather than drop it.
        raise OptionsUnsupportedOnRuntime(
            f"{', '.join(run_options)} cannot be applied on the ACP runtime {runtime!r}: "
            "SoftwareAgent.run starts a fresh harness session each time. Use "
            "SoftwareAgent.conversation() for several turns in one harness session, "
            f"`garuda run --runtime {runtime} --resume SESSION -t ...` to continue a saved "
            "session, or runtime='native'")
