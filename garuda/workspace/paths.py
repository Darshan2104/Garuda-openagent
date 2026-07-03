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


def resolve_workspace_path(workspace_root: Path, path: str, confine: bool) -> Path:
    candidate = Path(path)
    if candidate.is_absolute():
        full = Path(os.path.normpath(str(candidate)))
    else:
        full = Path(os.path.normpath(str(workspace_root / candidate)))
    if confine and full != workspace_root and not full.is_relative_to(workspace_root):
        raise PermissionError(
            f"Path {path} is outside the workspace root {workspace_root}. "
            "Pass an explicit workspace or disable confinement."
        )
    return full
