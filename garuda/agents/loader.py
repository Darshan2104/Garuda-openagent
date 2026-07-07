from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path

import yaml

from garuda.types import AgentConfig, DEFAULT_SYSTEM_PROMPT


@dataclass
class AgentProfile:
    name: str
    description: str = ""
    permission_mode: str = "smart"
    mode: str = "standard"
    tools: list[str] | None = None
    system_prompt: str | None = None
    tool_rules: dict[str, str] | None = None
    path_rules: dict[str, list[str]] | None = None
    bash_rules: dict[str, list[str]] | None = None
    max_turns: int = 200
    enable_tmux: bool = True
    marker_polling: bool = True
    enable_three_step_summary: bool = True
    max_context_tokens: int = 128_000
    proactive_summarize_threshold: int = 8000
    max_output_bytes: int = 30_720
    workspace_kind: str = "local"
    docker_image: str = "ubuntu:22.04"
    mcp_config_path: str | None = None
    skills: list[str] | None = None
    skills_dirs: list[str] | None = None
    subagent: bool = False
    reasoning_effort: str | None = None
    thinking_budget_tokens: int | None = None
    source_path: Path | None = None

    def to_agent_config(self) -> AgentConfig:
        return AgentConfig(
            max_turns=self.max_turns,
            mode=self.mode,
            permission_mode=self.permission_mode,
            system_prompt=self.system_prompt or DEFAULT_SYSTEM_PROMPT,
            allowed_tools=self.tools,
            enable_verifier=True,
            enable_tmux=self.enable_tmux,
            marker_polling=self.marker_polling,
            enable_three_step_summary=self.enable_three_step_summary,
            max_context_tokens=self.max_context_tokens,
            proactive_summarize_threshold=self.proactive_summarize_threshold,
            max_output_bytes=self.max_output_bytes,
            workspace_kind=self.workspace_kind,
            docker_image=self.docker_image,
            mcp_config_path=self.mcp_config_path,
            skills=self.skills,
            skills_dirs=self.skills_dirs,
            reasoning_effort=self.reasoning_effort,
            thinking_budget_tokens=self.thinking_budget_tokens,
        )


def _defaults_dir() -> Path:
    return Path(resources.files("garuda.agents")) / "defaults"


def _profile_names_in_dir(directory: Path) -> set[str]:
    names: set[str] = set()
    if not directory.exists():
        return names
    for path in directory.glob("*.yaml"):
        names.add(path.stem)
    for path in directory.glob("*.md"):
        names.add(path.stem)
    for path in directory.glob("agent.md"):
        names.add(directory.name)
    for path in directory.glob("**/agent.md"):
        names.add(path.parent.name)
    return names


def list_profiles(extra_dir: Path | None = None) -> list[str]:
    names = _profile_names_in_dir(_defaults_dir())
    if extra_dir:
        names.update(_profile_names_in_dir(extra_dir))
    return sorted(names)


def _profile_from_yaml(data: dict, name: str, source: Path | None = None) -> AgentProfile:
    return AgentProfile(
        name=data.get("name", name),
        description=data.get("description", ""),
        permission_mode=data.get("permission_mode", "smart"),
        mode=data.get("mode", "standard"),
        tools=data.get("tools"),
        system_prompt=data.get("system_prompt"),
        tool_rules=data.get("tool_rules"),
        path_rules=data.get("path_rules"),
        bash_rules=data.get("bash_rules"),
        max_turns=data.get("max_turns", 200),
        enable_tmux=data.get("enable_tmux", True),
        marker_polling=data.get("marker_polling", True),
        enable_three_step_summary=data.get("enable_three_step_summary", True),
        max_context_tokens=data.get("max_context_tokens", 128_000),
        proactive_summarize_threshold=data.get("proactive_summarize_threshold", 8000),
        max_output_bytes=data.get("max_output_bytes", 30_720),
        workspace_kind=data.get("workspace_kind", "local"),
        docker_image=data.get("docker_image", "ubuntu:22.04"),
        mcp_config_path=data.get("mcp_config_path"),
        skills=data.get("skills"),
        skills_dirs=data.get("skills_dirs"),
        subagent=data.get("subagent", False),
        reasoning_effort=data.get("reasoning_effort"),
        thinking_budget_tokens=data.get("thinking_budget_tokens"),
        source_path=source,
    )


def load_profile(name: str, extra_dir: Path | None = None) -> AgentProfile:
    """Load agent profile from YAML or agent.md (OpenCode-compatible)."""
    from garuda.agents.md_loader import load_agent_md

    candidates: list[Path] = []
    if extra_dir:
        candidates.extend(
            [
                extra_dir / f"{name}.yaml",
                extra_dir / f"{name}.md",
                extra_dir / f"{name}" / "agent.md",
            ]
        )
    candidates.extend(
        [
            _defaults_dir() / f"{name}.yaml",
            _defaults_dir() / f"{name}.md",
            _defaults_dir() / name / "agent.md",
        ]
    )
    for path in candidates:
        if not path.exists():
            continue
        if path.suffix == ".md":
            return load_agent_md(path)
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        return _profile_from_yaml(data, name, source=path)
    raise FileNotFoundError(f"Agent profile not found: {name}")


# Maximum characters of AGENTS.md/GARUDA.md content injected into the system prompt.
PROJECT_MEMORY_MAX_CHARS = 8000

# Project-memory file names checked in the workspace root; first found wins.
PROJECT_MEMORY_FILENAMES = ("AGENTS.md", "GARUDA.md")


def _project_memory_block(workspace_root: str | Path) -> str:
    """Return a prompt block from AGENTS.md/GARUDA.md in the workspace root, or ""."""
    root = Path(workspace_root)
    for name in PROJECT_MEMORY_FILENAMES:
        candidate = root / name
        try:
            if not candidate.is_file():
                continue
            content = candidate.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        content = content[:PROJECT_MEMORY_MAX_CHARS]
        return f"\n\n## Project instructions (from {name})\n{content}"
    return ""


def resolve_system_prompt(profile: AgentProfile, workspace_root: str | Path | None = None) -> str:
    """Build system prompt with optional skill injection and project memory."""
    from garuda.skills.loader import discover_skills, format_skills_prompt, load_skill

    base = profile.system_prompt or DEFAULT_SYSTEM_PROMPT
    skill_dirs: list[Path] = []
    if workspace_root:
        root = Path(workspace_root)
        # `.agent/skills` is the primary convention; the rest are back-compat.
        skill_dirs.extend(
            [
                root / ".agent" / "skills",
                root / ".garuda" / "skills",
                root / "skills",
                root / ".skills",
            ]
        )
    if profile.skills_dirs:
        skill_dirs.extend(Path(d) for d in profile.skills_dirs)
    skill_dirs.extend([Path(".agent/skills"), Path(".garuda/skills"), Path("skills")])

    discovered = discover_skills(*skill_dirs)
    if profile.skills:
        allowed = set(profile.skills)
        discovered = [s for s in discovered if s.name in allowed]
    skills_block = format_skills_prompt(discovered)
    prompt = f"{base}\n\n{skills_block}" if skills_block else base
    if workspace_root:
        prompt += _project_memory_block(workspace_root)
    return prompt
