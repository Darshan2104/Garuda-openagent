"""Who may set what: the permission ceiling for project-controlled profiles.

A repository can ship agent profiles under ``.agent/agents/`` (or ``.garuda/``).
Those files are workspace content, so running ``garuda`` in a cloned repository
must not let the repository raise its own permission mode — a project
``build.yaml`` with ``permission_mode: yolo`` used to replace the packaged
``build`` profile and turn off every approval prompt.

The ceiling is a user setting, read only from the global settings file::

    # ~/.agent/settings.yaml
    agents:
      project_ceiling: smart   # readonly | smart | auto | yolo (default: smart)

A project profile above the ceiling refuses before any model, MCP server or
project code starts. An explicit permission mode from the caller (CLI
``--permission-mode``, an SDK argument, the dashboard's capped request) is user
authority and is used as given.
"""

from __future__ import annotations

from pathlib import Path

from garuda.model.config import ConfigError

#: Nominal order, most to least restrictive. Used to compare a declared mode with
#: a ceiling of the same kind; explicit rules still ask or deny independently.
PERMISSION_ORDER = ("readonly", "smart", "auto", "yolo")
DEFAULT_PROJECT_CEILING = "smart"

PACKAGED = "packaged"
USER = "user"
PROJECT = "project"


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _real(path: str | Path) -> Path:
    return Path(path).expanduser().resolve()


def profile_authority(source_path: str | Path | None, workspace: str | Path) -> str:
    """Classify where a profile came from: packaged, user, or project.

    Location decides, not how the directory was named on the command line: a
    file inside the workspace is repository content even when ``--agents-dir``
    points at it. The user's global Garuda home wins over the workspace so a
    workspace rooted at ``$HOME`` does not demote the user's own files.
    """
    if source_path is None:
        return PACKAGED
    from importlib import resources

    from garuda.config.agent_home import global_settings_path

    source = _real(source_path)
    packaged_root = _real(Path(resources.files("garuda.agents")) / "defaults")
    if _is_within(source, packaged_root):
        return PACKAGED
    if _is_within(source, _real(global_settings_path()).parent):
        return USER
    if _is_within(source, _real(workspace)):
        return PROJECT
    return USER


def project_ceiling(global_settings: dict | None) -> str:
    """The user's ceiling for project profiles, validated."""
    agents = (global_settings or {}).get("agents")
    if agents is None:
        return DEFAULT_PROJECT_CEILING
    if not isinstance(agents, dict):
        raise ConfigError("global settings: `agents` must be a mapping")
    value = agents.get("project_ceiling", DEFAULT_PROJECT_CEILING)
    if value not in PERMISSION_ORDER:
        raise ConfigError(
            f"global settings: agents.project_ceiling must be one of "
            f"{', '.join(PERMISSION_ORDER)}; got {value!r}"
        )
    return value


def enforce_project_ceiling(
    *,
    profile_name: str,
    declared_mode: str,
    source_path: str | Path | None,
    workspace: str | Path,
    explicit_permission_mode: str | None,
    global_settings: dict | None,
) -> None:
    """Refuse a project profile whose own permission mode exceeds the ceiling.

    Raises :class:`ConfigError` with the ``agent.project_widening`` code. Does
    nothing when the caller chose the mode explicitly, or when the profile is
    packaged or the user's own.
    """
    if explicit_permission_mode is not None:
        return
    if profile_authority(source_path, workspace) != PROJECT:
        return
    ceiling = project_ceiling(global_settings)
    if declared_mode not in PERMISSION_ORDER:
        raise ConfigError(
            f"agent.project_widening: project profile {profile_name!r} ({source_path}) "
            f"sets an unknown permission_mode {declared_mode!r}"
        )
    if PERMISSION_ORDER.index(declared_mode) <= PERMISSION_ORDER.index(ceiling):
        return
    raise ConfigError(
        f"agent.project_widening: project profile {profile_name!r} ({source_path}) "
        f"sets permission_mode: {declared_mode}, above the project ceiling {ceiling!r}. "
        f"A repository cannot raise its own permissions. Fix: lower it in that file, "
        f"pass --permission-mode explicitly for this run, or raise "
        f"agents.project_ceiling in your global settings.yaml."
    )
