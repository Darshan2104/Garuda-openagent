"""Shared workspace-path resolution with lexical confinement.

Confinement is enforced on the **logical** (lexically-normalized) path — `..`
segments and absolute paths that climb outside the workspace root are rejected —
but symlinks are NOT resolved away first. This gives the file tools the same
reach as `bash` on any path the model can name: an in-workspace symlink such as
`corpus/ -> /data/corpus` is followed by both `read_file` and `bash`, while
`../etc/passwd` and `/etc/passwd` stay blocked. (Physical isolation of symlink
targets is the OS sandbox's job, not the resolver's.)
"""

import os
from pathlib import Path

from garuda.workspace.protocol import WorkspaceAccessDenied


def resolve_workspace_path(workspace_root: Path, path: str, confine: bool) -> Path:
    candidate = Path(path)
    if candidate.is_absolute():
        full = Path(os.path.normpath(str(candidate)))
    else:
        full = Path(os.path.normpath(str(workspace_root / candidate)))
    if confine and full != workspace_root and not full.is_relative_to(workspace_root):
        raise WorkspaceAccessDenied(
            f"Path {path} is outside the workspace root {workspace_root}. "
            "Pass an explicit workspace or disable confinement."
        )
    return full


def read_path_bytes(path: Path, max_bytes: int | None = None) -> bytes:
    if max_bytes is None:
        return path.read_bytes()
    if type(max_bytes) is not int or max_bytes < 0:
        raise ValueError("max_bytes must be a nonnegative integer")
    with path.open("rb") as handle:
        content = handle.read(max_bytes + 1)
    return check_read_size(content, max_bytes)


def check_read_size(content: bytes, max_bytes: int | None) -> bytes:
    if max_bytes is not None and len(content) > max_bytes:
        raise ValueError(f"File exceeds {max_bytes} byte limit")
    return content
