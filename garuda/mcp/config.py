import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

_ENV_PATTERN = re.compile(r"\$\{([^}]+)\}")


def _interpolate(value: str) -> str:
    def replace(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in os.environ:
            logger.warning(
                "MCP config references undefined environment variable ${%s}; substituting empty string",
                key,
            )
            return ""
        return os.environ[key]

    return _ENV_PATTERN.sub(replace, value)


@dataclass
class McpServerConfig:
    name: str
    transport: str = "stdio"
    command: str = ""
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    url: str | None = None


def _parse_mcp_servers(data: Any) -> list[dict]:
    """Normalize every supported top-level shape into a list of raw server dicts.

    Accepts:
      - ``servers``: list (Garuda YAML, current) — entries already carry ``name``
      - ``mcpServers``: dict keyed by server name (Cursor / Claude Desktop JSON)
      - ``mcp_servers``: dict keyed by name (snake_case alias)
      - any of the above nested under a top-level ``mcp`` key (some editors)

    For the dict forms the mapping key becomes the entry's ``name``. Malformed
    entries (non-mappings) are logged and skipped rather than raising. Returns
    ``[]`` for empty / ``None`` / non-mapping roots.
    """
    if not data:
        return []
    if not isinstance(data, dict):
        logger.warning(
            "MCP config root is not a mapping (got %s); no servers loaded",
            type(data).__name__,
        )
        return []

    # Some editors nest the whole block under a top-level "mcp" key. Flatten it,
    # letting any sibling top-level keys win on conflict.
    if isinstance(data.get("mcp"), dict):
        inner = data["mcp"]
        data = {**inner, **{k: v for k, v in data.items() if k != "mcp"}}

    entries: list[dict] = []

    servers = data.get("servers")
    if isinstance(servers, list):
        for entry in servers:
            if isinstance(entry, dict):
                entries.append(entry)
            else:
                logger.warning("Skipping malformed MCP server entry (not a mapping): %r", entry)
    elif servers is not None:
        logger.warning("MCP config 'servers' is not a list; ignoring it")

    for key in ("mcpServers", "mcp_servers"):
        block = data.get(key)
        if block is None:
            continue
        if not isinstance(block, dict):
            logger.warning("MCP config '%s' is not a mapping; ignoring it", key)
            continue
        for name, entry in block.items():
            if not isinstance(entry, dict):
                logger.warning("Skipping malformed MCP server %r (not a mapping)", name)
                continue
            merged = dict(entry)
            merged.setdefault("name", name)
            entries.append(merged)

    return entries


def _dict_to_server_configs(entries: list[dict]) -> list[McpServerConfig]:
    """Normalize raw server dicts into :class:`McpServerConfig` objects.

    Each entry is parsed under its own try/except so one malformed definition
    cannot abort loading the rest. ``${VAR}`` interpolation is applied to
    command / args / env string values for both JSON and YAML sources.
    """
    configs: list[McpServerConfig] = []
    for entry in entries:
        try:
            name = entry.get("name")
            if not name:
                logger.warning("Skipping MCP server entry with no name: %r", entry)
                continue
            command = _interpolate(str(entry.get("command", "") or ""))
            args = [_interpolate(str(a)) for a in (entry.get("args") or [])]
            env = {k: _interpolate(str(v)) for k, v in (entry.get("env") or {}).items()}
            configs.append(
                McpServerConfig(
                    name=name,
                    transport=entry.get("transport", "stdio"),
                    command=command,
                    args=args,
                    env=env,
                    url=entry.get("url"),
                )
            )
        except Exception as exc:
            server_name = entry.get("name") if isinstance(entry, dict) else entry
            logger.warning(
                "Skipping malformed MCP server entry %r (%s: %s)",
                server_name,
                type(exc).__name__,
                exc,
            )
    return configs


def load_mcp_config(path: str | Path) -> list[McpServerConfig]:
    """Load MCP server definitions from a YAML or JSON file.

    Branches on the file extension: ``.json`` is parsed as JSON, ``.yaml`` /
    ``.yml`` (and anything else) as YAML. Empty files, or files that parse to
    ``None``, are treated as "no servers" rather than crashing. A file that
    fails to parse is logged and yields no servers.
    """
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    try:
        if p.suffix.lower() == ".json":
            data = json.loads(text) if text.strip() else None
        else:
            data = yaml.safe_load(text)
    except Exception as exc:
        logger.warning(
            "Failed to parse MCP config %s (%s: %s); no servers loaded",
            p,
            type(exc).__name__,
            exc,
        )
        return []
    return _dict_to_server_configs(_parse_mcp_servers(data))


def _global_mcp_dir() -> Path:
    """Directory holding the global MCP config.

    Mirrors the convention in ``garuda.plugins.hooks.global_settings_path``:
    ``GARUDA_GLOBAL_SETTINGS`` points at the global settings *file*, so the
    global config directory is its parent. Falls back to ``~/.garuda``.
    """
    override = os.environ.get("GARUDA_GLOBAL_SETTINGS")
    if override:
        return Path(override).expanduser().parent
    return Path.home() / ".garuda"


def resolve_mcp_config(workspace: str | Path, explicit_path: str | None = None) -> str | None:
    """Resolve which MCP config file to load.

    An explicit path (from ``--mcp-config`` or a profile's ``mcp_config_path``)
    always wins. Otherwise the first existing conventional file is used:

      1. ``{workspace}/.garuda/mcp.json``
      2. ``{workspace}/.garuda/mcp.yaml``
      3. ``{workspace}/.cursor/mcp.json`` (drop-in compat for Cursor repos)
      4. ``{global}/mcp.json`` (``GARUDA_GLOBAL_SETTINGS`` dir or ``~/.garuda``)

    Returns the path string, logging at INFO which file was chosen, or ``None``
    when nothing is found (MCP stays disabled — the current behavior).
    """
    if explicit_path:
        return explicit_path
    ws = Path(workspace)
    candidates = [
        ws / ".garuda" / "mcp.json",
        ws / ".garuda" / "mcp.yaml",
        ws / ".cursor" / "mcp.json",
        _global_mcp_dir() / "mcp.json",
    ]
    for candidate in candidates:
        if candidate.is_file():
            logger.info("Loading MCP config from %s", candidate)
            return str(candidate)
    return None
