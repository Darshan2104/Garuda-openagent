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
    headers: dict[str, str] = field(default_factory=dict)


# Transport aliases seen across Cursor / Claude Desktop / VS Code / editor configs.
_TRANSPORT_ALIASES = {
    "stdio": "stdio",
    "http": "http",
    "streamable-http": "http",
    "streamable_http": "http",
    "streamablehttp": "http",
    "streamable": "http",
    "sse": "sse",
}


def normalize_transport(raw: str | None, has_url: bool = False) -> str:
    """Canonicalize a transport label to one of ``stdio`` / ``http`` / ``sse``.

    When no transport is given, infer ``http`` if a ``url`` is present (the
    Cursor/Claude convention: a ``url`` entry is a remote server) else ``stdio``.
    Unknown labels are passed through lower-cased so the client can reject them
    loudly rather than silently mis-routing.
    """
    if not raw:
        return "http" if has_url else "stdio"
    key = raw.strip().lower()
    return _TRANSPORT_ALIASES.get(key, key)


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
            raw_url = entry.get("url")
            url = _interpolate(str(raw_url)) if raw_url else None
            headers = {
                k: _interpolate(str(v)) for k, v in (entry.get("headers") or {}).items()
            }
            # A bearer token can be given as `auth`/`token`/`bearer` shorthand
            # instead of a full Authorization header.
            token = entry.get("auth") or entry.get("token") or entry.get("bearer")
            if token and "Authorization" not in headers:
                headers["Authorization"] = f"Bearer {_interpolate(str(token))}"
            transport = normalize_transport(
                entry.get("transport") or entry.get("type"), has_url=bool(url)
            )
            configs.append(
                McpServerConfig(
                    name=name,
                    transport=transport,
                    command=command,
                    args=args,
                    env=env,
                    url=url,
                    headers=headers,
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
    global config directory is its parent. Falls back to the standard global
    home (``~/.agent`` with ``~/.garuda`` back-compat).
    """
    override = os.environ.get("GARUDA_GLOBAL_SETTINGS")
    if override:
        return Path(override).expanduser().parent
    from garuda.config.agent_home import global_home_dir

    return global_home_dir()


def _mcp_merge_enabled(workspace: str | Path | None = None) -> bool:
    """Whether to merge project + global MCP configs (default: yes).

    Precedence: the ``GARUDA_MCP_MERGE`` env var (truthy/falsey) wins; else a
    project ``settings.yaml: mcp_merge: <bool>``; else the default (merge on).
    """
    raw = os.environ.get("GARUDA_MCP_MERGE", "").strip().lower()
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    if workspace is not None:
        try:
            from garuda.config.agent_home import resolve_agent_home

            val = resolve_agent_home(workspace).settings.get("mcp_merge")
            if isinstance(val, bool):
                return val
        except Exception:
            logger.debug("Failed to read mcp_merge setting", exc_info=True)
    return True


def resolve_mcp_config_paths(
    workspace: str | Path, explicit_path: str | None = None
) -> list[str]:
    """Resolve the ordered list of MCP config files to load.

    An explicit path (``--mcp-config`` or a profile's ``mcp_config_path``) always
    wins and is used alone. Otherwise the conventional locations are consulted:

      1. ``{workspace}/.agent/mcp.json`` (primary convention)
      2. ``{workspace}/.agent/mcp.yaml``
      3. ``{workspace}/.garuda/mcp.json`` (back-compat)
      4. ``{workspace}/.garuda/mcp.yaml``
      5. ``{workspace}/.cursor/mcp.json`` (drop-in compat for Cursor repos)
      6. ``{global}/mcp.json`` (``GARUDA_GLOBAL_SETTINGS`` dir or ``~/.garuda``)

    Default behavior now **merges**: the first project-scope file (1–5) and the
    global file (6) are both returned so their servers combine (project entries
    win on name collisions — see :func:`load_and_merge_mcp_configs`). Set
    ``GARUDA_MCP_MERGE=0`` (or ``mcp_merge: false`` in the project's settings.yaml)
    to force the legacy single-file behavior (first project file, else global).
    Returns ``[]`` when nothing is found (MCP stays disabled).
    """
    if explicit_path:
        return [explicit_path]
    from garuda.config.agent_home import resolve_agent_home

    ws = Path(workspace)
    # `.agent/mcp.*` then `.garuda/mcp.*` come from the shared agent home (one
    # definition of the convention roots); `.cursor/mcp.json` is editor compat.
    project_files = list(resolve_agent_home(ws).mcp_paths)
    cursor = ws / ".cursor" / "mcp.json"
    if cursor.is_file():
        project_files.append(str(cursor))
    global_candidate = _global_mcp_dir() / "mcp.json"

    project = project_files[0] if project_files else None
    has_global = global_candidate.is_file()

    if _mcp_merge_enabled(workspace):
        chosen = [p for p in (project, global_candidate if has_global else None) if p]
    else:
        chosen = [project] if project else ([global_candidate] if has_global else [])

    # De-dupe while preserving order (project before global).
    seen: set[str] = set()
    paths: list[str] = []
    for candidate in chosen:
        s = str(candidate)
        if s not in seen:
            seen.add(s)
            paths.append(s)
            logger.info("Loading MCP config from %s", candidate)
    return paths


def resolve_mcp_config(workspace: str | Path, explicit_path: str | None = None) -> str | None:
    """First MCP config path (backward-compatible single-path resolver).

    Prefer :func:`resolve_mcp_config_paths` for callers that support project+global
    merge; this thin wrapper returns just the first resolved path (or ``None``).
    """
    paths = resolve_mcp_config_paths(workspace, explicit_path)
    return paths[0] if paths else None


def load_and_merge_mcp_configs(paths: list[str | Path]) -> list[McpServerConfig]:
    """Load several config files and merge their servers, union by name.

    Earlier paths win on name collisions (callers pass project-scope files before
    the global one), so a repo-local server definition overrides a global one of
    the same name.
    """
    by_name: dict[str, McpServerConfig] = {}
    for path in paths:
        for cfg in load_mcp_config(path):
            by_name.setdefault(cfg.name, cfg)
    return list(by_name.values())
