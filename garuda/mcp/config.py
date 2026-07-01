from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass
class McpServerConfig:
    name: str
    transport: str = "stdio"
    command: str = ""
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)


def load_mcp_config(path: str | Path) -> list[McpServerConfig]:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    servers = data.get("servers", [])
    configs: list[McpServerConfig] = []
    for entry in servers:
        configs.append(
            McpServerConfig(
                name=entry["name"],
                transport=entry.get("transport", "stdio"),
                command=entry["command"],
                args=entry.get("args", []),
                env=entry.get("env", {}),
            )
        )
    return configs
