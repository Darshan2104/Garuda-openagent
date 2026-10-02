"""The ``no-edits`` guardrail (plan task C.10, #158). A guardrail, not confinement.

A role or step with ``write_policy: no-edits`` is asked not to change the
workspace, and its requests to are refused: a native run gets the
``readonly`` ceiling (edits and mutating commands denied), an ACP run's
approval requests for edits and commands are denied and recorded.

Nothing here stops a process from writing anyway. So after every descendant
has exited, the workspace is compared with a manifest taken before the run:
every entry — ignored files included — by type, mode, size, inode and change
time, symlinks by target, and the repository's refs, ``HEAD``, hooks and
config by content and its index by the staged entries. Entries are listed without following symlinks, within a
bound. Any difference, or a manifest that could not be taken or completed,
means **changes detected**: the step's outputs are withheld, a flow stops,
the evidence is recorded and nothing is reverted. Only a complete, identical
manifest reads "no changes detected".
"""

from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path

MAX_ENTRIES = 200_000
LABEL = "guardrail, not confinement"


class ManifestIncomplete(Exception):
    """The workspace could not be listed completely; never read as unchanged."""


def _digest(path: Path) -> str:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    h = hashlib.sha256()
    try:
        while chunk := os.read(fd, 1 << 20):
            h.update(chunk)
    finally:
        os.close(fd)
    return h.hexdigest()


def _index_digest(root: Path) -> str:
    """The staged entries, not the index file's bytes: `git status` may rewrite
    the file's stat cache without changing what is staged."""
    from garuda.workspace.snapshot_proto import git

    result = git(root, "ls-files", "-s", "-z", check=False)
    if result.returncode != 0:
        raise ManifestIncomplete("cannot read the index")
    return hashlib.sha256(result.stdout).hexdigest()


def _skip_git(rel: str) -> bool:
    return (rel in (".git/objects", ".git/logs") or rel.endswith(".lock")
            or rel.startswith((".git/objects/", ".git/logs/")))


def manifest(root, *, max_entries: int = MAX_ENTRIES) -> dict[str, tuple]:
    """``{relative path: facts}`` for every entry under ``root``.

    Outside ``.git`` every entry is described by type, mode, inode and change
    time (plus size and mtime for files, the target for symlinks). Inside
    ``.git`` only content counts — refs, ``HEAD``, config, hooks by digest and
    the index by its staged entries — and lock files, objects and reflogs are
    skipped (object writes surface through the refs).
    """
    root = Path(root)
    out: dict[str, tuple] = {}
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            entries = list(os.scandir(current))
        except OSError as exc:
            raise ManifestIncomplete(f"cannot list {current}: {exc}") from exc
        for entry in entries:
            if len(out) >= max_entries:
                raise ManifestIncomplete(f"more than {max_entries} entries")
            path = Path(entry.path)
            rel = path.relative_to(root).as_posix()
            in_git = rel == ".git" or rel.startswith(".git/")
            if in_git and _skip_git(rel):
                continue
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError as exc:
                raise ManifestIncomplete(f"cannot stat {rel}: {exc}") from exc
            kind, mode = stat.S_IFMT(info.st_mode), stat.S_IMODE(info.st_mode)
            if stat.S_ISLNK(info.st_mode):
                out[rel] = (kind, mode, os.readlink(path))
            elif stat.S_ISDIR(info.st_mode):
                out[rel] = (kind, mode) if in_git else (kind, mode, info.st_ino,
                                                         info.st_ctime_ns)
                stack.append(path)
            elif in_git:
                out[rel] = (kind, mode, _index_digest(root) if rel == ".git/index"
                            else _digest(path))
            else:
                out[rel] = (kind, mode, info.st_ino, info.st_ctime_ns, info.st_size,
                            info.st_mtime_ns)
    return out


@dataclass
class GuardResult:
    changed: list[str] = field(default_factory=list)
    reason: str = ""

    @property
    def unchanged(self) -> bool:
        return not self.changed and not self.reason

    def summary(self) -> str:
        if self.unchanged:
            return f"no-edits: no changes detected ({LABEL})"
        what = f"{len(self.changed)} changed path(s)" if self.changed else self.reason
        return f"no-edits: changes detected — {what}; outputs withheld ({LABEL})"

    def record(self) -> dict:
        return {"result": "unchanged" if self.unchanged else "changed",
                "changed": self.changed[:100], "changed_count": len(self.changed),
                "reason": self.reason, "label": LABEL}


class NoEditsGuard:
    """Take the manifest before a run and compare it after."""

    def __init__(self, workspace):
        self.workspace = Path(workspace).resolve()
        try:
            self.before: dict | None = manifest(self.workspace)
            self.error = ""
        except ManifestIncomplete as exc:
            self.before, self.error = None, f"no baseline: {exc}"

    def check(self) -> GuardResult:
        if self.before is None:
            return GuardResult(reason=self.error)
        try:
            after = manifest(self.workspace)
        except ManifestIncomplete as exc:
            return GuardResult(reason=f"attribution unknown: {exc}")
        paths = set(self.before) | set(after)
        changed = sorted(p for p in paths if self.before.get(p) != after.get(p))
        return GuardResult(changed=changed)


async def deny_all(_action: str) -> bool:
    """The approval answer for a no-edits ACP role: edits and commands are refused."""
    return False
