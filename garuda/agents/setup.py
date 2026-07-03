"""Shared agent profile setup for run, serve, recipes, and eval entry points."""

from pathlib import Path

from garuda.agents.loader import AgentProfile, load_profile, resolve_system_prompt
from garuda.core.permissions import PermissionEngine
from garuda.core.rigorous import create_agent
from garuda.mcp.config import resolve_mcp_config_paths
from garuda.tools import build_toolkit
from garuda.types import AgentConfig


async def prepare_agent_run(
    agent_name: str,
    *,
    workspace: str,
    agents_dir: Path | None = None,
    mcp_config_path: str | None = None,
    mode: str = "standard",
    approval_handler=None,
) -> tuple[AgentProfile, AgentConfig, PermissionEngine, list, object, object | None]:
    """Load profile, resolve skills, build toolkit, and return run dependencies."""
    profile = load_profile(agent_name, extra_dir=agents_dir)
    config = profile.to_agent_config()
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
    tools, mcp_manager = await build_toolkit(profile.tools, mcp_paths)
    agent = create_agent(profile.name, mode=mode)
    return profile, config, permissions, tools, agent, mcp_manager
