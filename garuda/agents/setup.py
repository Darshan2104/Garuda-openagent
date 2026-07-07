"""Shared agent profile setup for run, serve, recipes, and eval entry points."""

from pathlib import Path

from garuda.agents.loader import AgentProfile, load_profile, resolve_system_prompt
from garuda.core.permissions import PermissionEngine
from garuda.core.rigorous import create_agent
from garuda.mcp.config import resolve_mcp_config_paths
from garuda.tools import build_toolkit
from garuda.tools.protocol import Tool
from garuda.types import AgentConfig


async def prepare_agent_run(
    agent_name: str,
    *,
    workspace: str,
    agents_dir: Path | list[Path] | None = None,
    mcp_config_path: str | None = None,
    mode: str | None = None,
    approval_handler=None,
    extra_tools: list[Tool] | None = None,
    load_project_tools: bool | None = None,
) -> tuple[AgentProfile, AgentConfig, PermissionEngine, list, object, object | None]:
    """Load profile, resolve skills, build toolkit, and return run dependencies."""
    from garuda.config.agent_home import resolve_agents_dirs

    # Default the profiles dirs to the project's `.agent/agents` then `.garuda/agents`
    # when the caller didn't pass any. Idempotent: an explicit dir/list is kept as-is.
    agents_dirs = resolve_agents_dirs(workspace, agents_dir)
    profile = load_profile(agent_name, extra_dir=agents_dirs)
    config = profile.to_agent_config()
    # Only override the profile's own mode when a caller explicitly asked for one,
    # so a `mode: rigorous` profile isn't silently downgraded to standard.
    if mode:
        config.mode = mode
    config.system_prompt = resolve_system_prompt(profile, workspace)
    mcp_paths = resolve_mcp_config_paths(workspace, mcp_config_path or config.mcp_config_path)
    permissions = PermissionEngine(
        mode=config.permission_mode,
        tool_rules=profile.tool_rules,
        path_rules=profile.path_rules,
        bash_rules=profile.bash_rules,
        approval_handler=approval_handler,
    )
    tools, mcp_manager = await build_toolkit(
        profile.tools,
        mcp_paths,
        extra_tools=extra_tools,
        workspace=workspace,
        load_project_tools=load_project_tools,
        mcp_servers=profile.mcp_servers,
    )
    agent = create_agent(profile.name, mode=config.mode)
    return profile, config, permissions, tools, agent, mcp_manager
