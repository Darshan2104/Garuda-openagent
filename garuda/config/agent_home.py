"""Project agent-home discovery: the ``.agent/`` convention folder.

One directory at the workspace root holds everything an agent uses — profiles,
skills, custom tools, and MCP server definitions — so assets can be dropped into
a single place and picked up automatically::

    .agent/
      agents/          # profiles: build.yaml, myagent.md, myagent/agent.md
      skills/          # SKILL.md files
      tools/           # *.py modules exporting custom Garuda tools (opt-in)
      mcp.json         # (or mcp.yaml) MCP server definitions
      settings.yaml    # optional per-project defaults

``.agent/`` is the primary convention; ``.garuda/`` is a back-compat alias whose
contents apply underneath (``.agent/`` wins on conflict). This module only
*resolves* the layout; the existing loaders (profiles, skills, MCP) consume the
resolved subpaths.

**Trust boundary.** A project's own ``settings.yaml`` is workspace-controlled —
running ``garuda`` inside a cloned repo must not let that repo's own config
authorize the harness to execute the repo's code. So anything that runs
arbitrary code (``load_project_tools``, project hook commands — see
:mod:`garuda.plugins.hooks`) is gated on the GLOBAL ``settings.yaml``
(``global_settings_path()``), which only the user controls, never on the
project-level ``settings`` merged here.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
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
    settings: dict  # merged PROJECT settings.yaml (workspace-controlled)
    global_settings: dict = field(default_factory=dict)  # user-level settings.yaml (trust anchor)

    @property
    def agents_dir(self) -> Path | None:
        """First existing ``<root>/agents`` dir (profiles), or ``None``."""
        return _first_dir(r / "agents" for r in self.roots)

    @property
    def agents_dirs(self) -> list[Path]:
        """``<root>/agents`` for every root, precedence order (``.agent`` first).

        Existence is checked by the profile loader per candidate, so both
        ``.agent/agents`` and ``.garuda/agents`` are searched — matching how
        skills merge across roots.
        """
        return [r / "agents" for r in self.roots]

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
        """Opt-in flag for importing ``.agent/tools/*.py`` (executes repo code).

        Sourced from the GLOBAL settings.yaml only (``~/.agent/settings.yaml``),
        never the project's own ``settings.yaml`` — otherwise a cloned repo could
        self-enable execution of its own tool modules just by shipping this key.
        A user who trusts a given project should set it globally, or pass the
        per-run ``--load-project-tools`` / SDK ``load_project_tools`` override.
        """
        return bool(self.global_settings.get("load_project_tools", False))

    @property
    def trust_project_hooks(self) -> bool:
        """Opt-in flag (GLOBAL settings.yaml only) for running shell-command hooks
        declared in a project's own ``settings.yaml``.

        Same trust-boundary reasoning as :attr:`load_project_tools`: a
        ``before_tool``/``session_start`` hook is an arbitrary shell command, so a
        cloned repo's own config must not be able to self-authorize running it.
        """
        return bool(self.global_settings.get("trust_project_hooks", False))


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
    return AgentHome(
        workspace=ws,
        roots=roots,
        settings=_load_settings(roots),
        global_settings=_load_global_settings(),
    )


def resolve_agents_dir(
    workspace: str | Path, explicit: str | Path | None = None
) -> Path | None:
    """Profiles directory (single): an explicit ``--agents-dir`` wins, else the
    home's first ``agents/``. Prefer :func:`resolve_agents_dirs` for new call
    sites so both ``.agent`` and ``.garuda`` are searched.

    Idempotent — safe to call at an entry point and again inside shared setup.
    """
    if explicit:
        return Path(explicit)
    return resolve_agent_home(workspace).agents_dir


def resolve_agents_dirs(
    workspace: str | Path,
    explicit: str | Path | list[str | Path] | None = None,
) -> list[Path]:
    """Ordered profiles dirs: explicit ``--agents-dir`` wins (kept as-is), else the
    home's ``.agent/agents`` then ``.garuda/agents``.

    Idempotent for lists so it can be re-applied inside shared setup. This is the
    standard resolver — profiles, like skills, search every convention root.
    """
    if explicit:
        if isinstance(explicit, (list, tuple)):
            return [Path(p) for p in explicit]
        return [Path(explicit)]
    return resolve_agent_home(workspace).agents_dirs


# --- Global (user-level) home, mirroring the project convention -----------------

GLOBAL_HOME_DIRS = (".agent", ".garuda")


def global_home_dir() -> Path:
    """The user-level home dir: ``~/.agent`` (standard) with ``~/.garuda`` back-compat.

    Prefers an existing ``~/.garuda`` when ``~/.agent`` is absent so existing
    installs keep their global settings/sessions; otherwise defaults to the new
    ``~/.agent`` standard. Callers that honor an explicit env override should apply
    it before falling back here.
    """
    home = Path.home()
    for name in GLOBAL_HOME_DIRS:
        if (home / name).is_dir():
            return home / name
    return home / ".agent"


def global_settings_path() -> Path:
    """Path to the user-level ``settings.yaml`` — the trust anchor for anything that
    executes code sourced from a *project's own* config (``load_project_tools``,
    project hook commands): those flags are honored only when set here, never in
    the workspace's own ``settings.yaml``, so a cloned repo cannot self-authorize
    running its own code. Override with ``GARUDA_GLOBAL_SETTINGS``.
    """
    override = os.environ.get("GARUDA_GLOBAL_SETTINGS")
    if override:
        return Path(override).expanduser()
    return global_home_dir() / "settings.yaml"


def _load_global_settings() -> dict:
    path = global_settings_path()
    if not path.is_file():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        logger.warning("Failed to parse global agent settings %s", path, exc_info=True)
        return {}
    return data if isinstance(data, dict) else {}
