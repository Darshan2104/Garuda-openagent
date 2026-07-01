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

    def to_agent_config(self) -> AgentConfig:
        return AgentConfig(
            max_turns=self.max_turns,
            mode=self.mode,
            permission_mode=self.permission_mode,
            system_prompt=self.system_prompt or DEFAULT_SYSTEM_PROMPT,
            allowed_tools=self.tools,
            enable_verifier=True,
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
            )
    raise FileNotFoundError(f"Agent profile not found: {name}")
