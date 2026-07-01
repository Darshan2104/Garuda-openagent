"""Load OpenCode-style agent.md profiles with YAML frontmatter."""

from pathlib import Path

from garuda.agents.frontmatter import parse_frontmatter
from garuda.agents.loader import AgentProfile


def load_agent_md(path: str | Path) -> AgentProfile:
    """Parse an agent.md file into an AgentProfile."""
    target = Path(path)
    meta, body = parse_frontmatter(target.read_text(encoding="utf-8"))
    tools = meta.get("tools")
    if isinstance(tools, str):
        tools = [tools]
    skills = meta.get("skills")
    if isinstance(skills, str):
        skills = [skills]
    return AgentProfile(
        name=meta.get("name", target.stem),
        description=meta.get("description", ""),
        permission_mode=meta.get("permission_mode", "smart"),
        mode=meta.get("mode", "standard"),
        tools=tools,
        system_prompt=body or meta.get("system_prompt"),
        tool_rules=meta.get("tool_rules"),
        path_rules=meta.get("path_rules"),
        bash_rules=meta.get("bash_rules"),
        max_turns=meta.get("max_turns", 200),
        enable_tmux=meta.get("enable_tmux", True),
        marker_polling=meta.get("marker_polling", True),
        enable_three_step_summary=meta.get("enable_three_step_summary", True),
        max_context_tokens=meta.get("max_context_tokens", 128_000),
        proactive_summarize_threshold=meta.get("proactive_summarize_threshold", 8000),
        max_output_bytes=meta.get("max_output_bytes", 30_720),
        workspace_kind=meta.get("workspace_kind", "local"),
        docker_image=meta.get("docker_image", "ubuntu:22.04"),
        mcp_config_path=meta.get("mcp_config_path"),
        skills=skills,
        skills_dirs=meta.get("skills_dirs"),
        subagent=meta.get("subagent", False),
        source_path=target,
    )
