from dataclasses import dataclass
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
    max_turns: int = 200
    enable_tmux: bool = True
    marker_polling: bool = True
    enable_three_step_summary: bool = True
    workspace_kind: str = "local"
    docker_image: str = "ubuntu:22.04"
    mcp_config_path: str | None = None

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
            workspace_kind=self.workspace_kind,
            docker_image=self.docker_image,
            mcp_config_path=self.mcp_config_path,
        )


def _defaults_dir() -> Path:
    return Path(resources.files("garuda.agents")) / "defaults"


def list_profiles(extra_dir: Path | None = None) -> list[str]:
    names = {path.stem for path in _defaults_dir().glob("*.yaml")}
    if extra_dir and extra_dir.exists():
        names.update(path.stem for path in extra_dir.glob("*.yaml"))
    return sorted(names)


def load_profile(name: str, extra_dir: Path | None = None) -> AgentProfile:
    candidates = [_defaults_dir() / f"{name}.yaml"]
    if extra_dir:
        candidates.insert(0, extra_dir / f"{name}.yaml")
    for path in candidates:
        if path.exists():
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
            return AgentProfile(
                name=data.get("name", name),
                description=data.get("description", ""),
                permission_mode=data.get("permission_mode", "smart"),
                mode=data.get("mode", "standard"),
                tools=data.get("tools"),
                system_prompt=data.get("system_prompt"),
                tool_rules=data.get("tool_rules"),
                max_turns=data.get("max_turns", 200),
                enable_tmux=data.get("enable_tmux", True),
                marker_polling=data.get("marker_polling", True),
                enable_three_step_summary=data.get("enable_three_step_summary", True),
                workspace_kind=data.get("workspace_kind", "local"),
                docker_image=data.get("docker_image", "ubuntu:22.04"),
                mcp_config_path=data.get("mcp_config_path"),
            )
    raise FileNotFoundError(f"Agent profile not found: {name}")
