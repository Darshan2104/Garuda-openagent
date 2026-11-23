"""Filesystem-only Git checkout detection for project scoping."""

import os
from pathlib import Path


def git_checkout_root(checkout: Path) -> Path | None:
    """Return the project root if this directory has a usable Git layout."""
    marker = checkout / ".git"
    try:
        git_dir = marker
        if marker.is_file():
            prefix, _, target = marker.read_text(encoding="utf-8").strip().partition(":")
            if prefix != "gitdir" or not target.strip():
                return None
            git_dir = checkout / target.strip()

        if not os.access(git_dir, os.R_OK | os.X_OK) or not (git_dir / "HEAD").is_file():
            return None
        common_dir = git_dir
        common_file = git_dir / "commondir"
        has_common_dir = common_file.is_file()
        if has_common_dir:
            target = common_file.read_text(encoding="utf-8").strip()
            if not target:
                return None
            common_dir = (git_dir / target).resolve(strict=True)
            if not os.access(common_dir, os.R_OK | os.X_OK):
                return None
        if not (common_dir / "objects").is_dir() or not (common_dir / "refs").is_dir():
            return None
    except (OSError, RuntimeError, ValueError):
        return None

    if git_dir != marker and has_common_dir:
        return common_dir.parent if common_dir.name == ".git" else common_dir
    return checkout
