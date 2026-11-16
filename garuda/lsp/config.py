"""Trusted language-server configuration. Reading it never launches anything.

A language server is an executable Garuda starts and feeds workspace files to,
so choosing one is an execution grant. Only the user's global settings file
(``~/.agent/settings.yaml``, or ``GARUDA_GLOBAL_SETTINGS``) may name one:

.. code-block:: yaml

    language_servers:
      - name: pyright
        command: pyright-langserver
        args: ["--stdio"]
        extensions: [py, pyi]

A ``language_servers`` key in a project's own ``settings.yaml`` is repository
content: it is reported and ignored, never merged, so a cloned repository can
neither add a server nor change the command or arguments of one the user
configured. With no global entry the feature is off and runs are unchanged.
"""

from __future__ import annotations

import logging
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

SETTINGS_KEY = "language_servers"
DEFAULT_STARTUP_TIMEOUT = 20.0
DEFAULT_REQUEST_TIMEOUT = 15.0
MAX_TIMEOUT = 120.0
UNTRUSTED_CODE = "agent.untrusted_project_language_server"


@dataclass(frozen=True)
class LanguageServerConfig:
    name: str
    command: str
    args: tuple[str, ...] = ()
    extensions: tuple[str, ...] = ()  # lower-case, without the dot
    language_id: str = ""
    env: dict[str, str] = field(default_factory=dict)
    startup_timeout: float = DEFAULT_STARTUP_TIMEOUT
    request_timeout: float = DEFAULT_REQUEST_TIMEOUT

    @property
    def command_line(self) -> str:
        return " ".join([self.command, *self.args])


@dataclass(frozen=True)
class LanguageServerSettings:
    """Usable servers plus everything that was refused, for reporting."""

    servers: tuple[LanguageServerConfig, ...] = ()
    problems: tuple[str, ...] = ()
    ignored_project: bool = False
    source: str = ""


def _timeout(raw, default: float, key: str) -> float:
    if raw is None:
        return default
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not 0 < raw <= MAX_TIMEOUT:
        raise ValueError(f"{key} must be a number of seconds in (0, {MAX_TIMEOUT:.0f}]")
    return float(raw)


def _parse_entry(entry) -> LanguageServerConfig:
    if not isinstance(entry, dict):
        raise ValueError("entry is not a mapping")
    name = entry.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("entry has no name")
    command = entry.get("command")
    if not isinstance(command, str) or not command.strip():
        raise ValueError(f"{name}: command must be a non-empty string")
    command = os.path.expanduser(command.strip())
    if "/" in command and not os.path.isabs(command):
        # Relative to what? Not the workspace: that would let repository content
        # choose the executable. Require a PATH name or an absolute path.
        raise ValueError(f"{name}: command must be a name on PATH or an absolute path")
    args = entry.get("args", [])
    if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
        raise ValueError(f"{name}: args must be a list of strings")
    raw_ext = entry.get("extensions")
    if not isinstance(raw_ext, list) or not raw_ext or not all(
            isinstance(e, str) and e.strip(". ") for e in raw_ext):
        raise ValueError(f"{name}: extensions must be a non-empty list such as [py, pyi]")
    extensions = tuple(dict.fromkeys(e.strip().lstrip(".").lower() for e in raw_ext))
    language_id = entry.get("language_id", extensions[0])
    if not isinstance(language_id, str) or not language_id:
        raise ValueError(f"{name}: language_id must be a non-empty string")
    env = entry.get("env", {})
    if not isinstance(env, dict):
        raise ValueError(f"{name}: env must be a mapping")
    return LanguageServerConfig(
        name=name.strip(),
        command=command,
        args=tuple(args),
        extensions=extensions,
        language_id=language_id,
        env={str(k): str(v) for k, v in env.items()},
        startup_timeout=_timeout(
            entry.get("startup_timeout"), DEFAULT_STARTUP_TIMEOUT, f"{name}: startup_timeout"),
        request_timeout=_timeout(
            entry.get("request_timeout"), DEFAULT_REQUEST_TIMEOUT, f"{name}: request_timeout"),
    )


def parse_language_servers(raw) -> tuple[list[LanguageServerConfig], list[str]]:
    """Validate the global ``language_servers`` list.

    A malformed entry is skipped and reported; it never disables the others. An
    extension claimed by an earlier server keeps that server: the later entry is
    refused rather than silently sharing or shadowing it.
    """
    if raw is None:
        return [], []
    if not isinstance(raw, list):
        return [], [f"{SETTINGS_KEY} must be a list of server entries"]
    servers: list[LanguageServerConfig] = []
    problems: list[str] = []
    owners: dict[str, str] = {}
    names: set[str] = set()
    for entry in raw:
        try:
            server = _parse_entry(entry)
        except ValueError as exc:
            problems.append(f"skipped language server: {exc}")
            continue
        if server.name in names:
            problems.append(f"skipped language server {server.name}: duplicate name")
            continue
        clash = [e for e in server.extensions if e in owners]
        if clash:
            problems.append(
                f"skipped language server {server.name}: extension(s) "
                f"{', '.join('.' + e for e in clash)} already served by {owners[clash[0]]}"
            )
            continue
        names.add(server.name)
        owners.update({e: server.name for e in server.extensions})
        servers.append(server)
    return servers, problems


def load_language_servers(workspace: str | Path) -> LanguageServerSettings:
    """Resolve the language servers a run in ``workspace`` may use.

    Reads the global and project settings files only.
    """
    from garuda.config.agent_home import global_settings_path, resolve_agent_home

    home = resolve_agent_home(workspace)
    problems: list[str] = []
    ignored = isinstance(home.settings, dict) and SETTINGS_KEY in home.settings
    if ignored:
        problems.append(
            f"{UNTRUSTED_CODE}: ignored {SETTINGS_KEY} in the project's settings.yaml; "
            "language servers are configured only in the global settings file"
        )
        logger.warning("%s", problems[-1])
    servers, more = parse_language_servers((home.global_settings or {}).get(SETTINGS_KEY))
    for problem in more:
        logger.warning("%s", problem)
    problems.extend(more)
    return LanguageServerSettings(
        servers=tuple(servers),
        problems=tuple(problems),
        ignored_project=ignored,
        source=str(global_settings_path()),
    )


def locate_executable(server: LanguageServerConfig) -> str | None:
    """Where ``command`` resolves on PATH, without running it."""
    path = server.env.get("PATH", os.environ.get("PATH"))
    return shutil.which(server.command, path=path)
