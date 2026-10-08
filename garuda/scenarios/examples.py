"""Materialize the installed reconnect project into an exclusively new directory."""

from __future__ import annotations

import os
import subprocess
from importlib.resources import files
from pathlib import Path

from garuda.scenarios.types import StarterError


def materialize_reconnect(directory: str) -> dict:
    try:
        target = Path(directory).expanduser().absolute()
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise StarterError("starter.example_invalid",
                           "choose a directory with a resolvable path and home directory") from exc
    if target.exists() or target.is_symlink():
        raise StarterError("starter.example_conflict", "choose a new directory; existing targets are never overwritten")
    if not target.parent.is_dir():
        raise StarterError("starter.example_invalid", "the selected directory's parent must already exist")
    for ancestor in target.parents:
        if ancestor.is_symlink():
            raise StarterError("starter.example_invalid", "choose a directory without symlinked parents")
        git = ancestor / ".git"
        if git.exists() or git.is_symlink():
            raise StarterError("starter.example_conflict", "choose a directory outside an existing Git repository")
    resource = files("examples.workflows.reconnect")
    # Installed package resources only. No project templates, scripts or config.
    names = ("client.py", "test_client.py", "pyproject.toml", "AGENTS.md", ".gitignore")
    contents = {name: resource.joinpath(name).read_bytes() for name in names}
    # mkdir is exclusive even if another process creates the target after preflight.
    target.mkdir()
    for name, content in contents.items():
        with (target / name).open("xb") as destination:
            destination.write(content)
    # Repository-directed environment and global Git hooks/config must not make
    # this local example command operate on another repository or run user code.
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
    command = ["git", "-c", "core.hooksPath=" + os.devnull,
               "-c", "commit.gpgsign=false", "-c", "user.name=Garuda example",
               "-c", "user.email=example@localhost"]
    try:
        for args in (["init", "--initial-branch=main"], ["add", "--", *names],
                     ["commit", "-m", "Initial reconnect example"]):
            subprocess.run([*command, *args], cwd=target, env=env, check=True,
                           capture_output=True, text=True, timeout=30)
    except (subprocess.SubprocessError, OSError) as exc:
        raise StarterError("starter.example_git_failed",
                           "local Git initialization failed; inspect the new example directory before retrying") from exc
    return {"example": "reconnect", "directory": str(target), "files": list(names),
            "check": "python -m pytest", "next_action": "Run garuda init, then garuda starter run plan-change --goal 'Add reconnect status'"}
