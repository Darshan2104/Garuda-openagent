import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

_ENV_PATTERN = re.compile(r"\$\{([^}]+)\}")


def _interpolate(value: str) -> str:
    def replace(match: re.Match[str]) -> str:
        key = match.group(1)
        return os.environ.get(key, "")

    return _ENV_PATTERN.sub(replace, value)


@dataclass
class McpServerConfig:
    name: str
    transport: str = "stdio"
    command: str = ""
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    url: str | None = None


def load_mcp_config(path: str | Path) -> list[McpServerConfig]:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    servers = data.get("servers", [])
    configs: list[McpServerConfig] = []
    for entry in servers:
        command = _interpolate(entry.get("command", ""))
        args = [_interpolate(str(a)) for a in entry.get("args", [])]
        env = {k: _interpolate(str(v)) for k, v in entry.get("env", {}).items()}
        configs.append(
            McpServerConfig(
                name=entry["name"],
                transport=entry.get("transport", "stdio"),
                command=command,
                args=args,
                env=env,
                url=entry.get("url"),
            )
        )
    return configs
