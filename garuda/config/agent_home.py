"""Project agent-home discovery: the ``.agent/`` convention folder.

One directory at the workspace root holds everything an agent uses — profiles,
skills, custom tools, and MCP server definitions — so assets can be dropped into
a single place and picked up automatically::

    .agent/
      agents/          # profiles: build.yaml, myagent.md, myagent/agent.md
      skills/          # SKILL.md files
      tools/           # *.py modules exporting custom Garuda tools (opt-in)
      mcp.json         # (or mcp.yaml) MCP server definitions
      settings.yaml    # optional per-project defaults / trust flags

``.agent/`` is the primary convention; ``.garuda/`` is a back-compat alias whose
contents apply underneath (``.agent/`` wins on conflict). This module only
*resolves* the layout; the existing loaders (profiles, skills, MCP) consume the
resolved subpaths.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

# Convention folders at the workspace root, in precedence order (first wins).
AGENT_HOME_DIRS = (".agent", ".garuda")
_MCP_FILENAMES = ("mcp.json", "mcp.yaml", "mcp.yml")
_SETTINGS_FILENAMES = ("settings.yaml", "settings.yml")


@dataclass(frozen=True)
class AgentHome:
    """Resolved project agent-home: the existing convention roots plus settings."""

    workspace: Path
    roots: tuple[Path, ...]  # existing home dirs, precedence order (.agent before .garuda)
    settings: dict

    @property
    def agents_dir(self) -> Path | None:
        """First existing ``<root>/agents`` dir (profiles), or ``None``."""
        return _first_dir(r / "agents" for r in self.roots)

    @property
    def skills_dirs(self) -> list[Path]:
        """``<root>/skills`` for every root (existence checked by the discoverer)."""
        return [r / "skills" for r in self.roots]

    @property
    def tools_dirs(self) -> list[Path]:
        """``<root>/tools`` for every existing root (file-based tool loading)."""
        return [r / "tools" for r in self.roots]

    @property
    def mcp_paths(self) -> list[str]:
        """First MCP config file found under each root, project-precedence order."""
        paths: list[str] = []
        for root in self.roots:
            for name in _MCP_FILENAMES:
                candidate = root / name
                if candidate.is_file():
                    paths.append(str(candidate))
                    break
        return paths

    @property
    def load_project_tools(self) -> bool:
        """Opt-in flag (``settings.yaml``) for importing ``.agent/tools/*.py``.

        Off by default: importing project modules executes repo code, so it must
        be explicitly enabled per project.
        """
        return bool(self.settings.get("load_project_tools", False))


def _first_dir(candidates) -> Path | None:
    for candidate in candidates:
        path = Path(candidate)
        if path.is_dir():
            return path
    return None


def _load_settings(roots: tuple[Path, ...]) -> dict:
    """Merge ``settings.yaml`` across roots; lower-precedence first so ``.agent`` wins."""
    merged: dict = {}
    for root in reversed(roots):
        for name in _SETTINGS_FILENAMES:
            path = root / name
            if not path.is_file():
                continue
            try:
                data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            except Exception:
                logger.warning("Failed to parse agent settings %s", path, exc_info=True)
                data = {}
            if isinstance(data, dict):
                merged.update(data)
            break
    return merged


def resolve_agent_home(workspace: str | Path) -> AgentHome:
    """Resolve the ``.agent/`` (and back-compat ``.garuda/``) home for a workspace."""
    ws = Path(workspace)
    roots = tuple(ws / name for name in AGENT_HOME_DIRS if (ws / name).is_dir())
    return AgentHome(workspace=ws, roots=roots, settings=_load_settings(roots))


def resolve_agents_dir(
    workspace: str | Path, explicit: str | Path | None = None
) -> Path | None:
    """Profiles directory: an explicit ``--agents-dir`` wins, else the home's ``agents/``.

    Idempotent — safe to call at an entry point and again inside shared setup.
    """
    if explicit:
        return Path(explicit)
    return resolve_agent_home(workspace).agents_dir
