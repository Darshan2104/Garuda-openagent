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
MAX_METADATA_FILE_BYTES = 128 * 1024 * 1024
LABEL = "guardrail, not confinement"


class ManifestIncomplete(Exception):
    """The workspace could not be listed completely; never read as unchanged."""


def _digest(path: Path) -> str:
    flags = os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_METADATA_FILE_BYTES:
            raise ManifestIncomplete("Git metadata is not a bounded regular file")
        h = hashlib.sha256()
        remaining = MAX_METADATA_FILE_BYTES
        while chunk := os.read(fd, min(remaining + 1, 1 << 20)):
            remaining -= len(chunk)
            if remaining < 0:
                raise ManifestIncomplete("Git metadata exceeded its byte limit")
            h.update(chunk)
        return h.hexdigest()
    finally:
        os.close(fd)


def _index_digest(root: Path, index: Path | None = None) -> str:
    """The staged entries, not the index file's bytes: `git status` may rewrite
    the file's stat cache without changing what is staged."""
    from garuda.workspace.snapshot_proto import git

    result = git(root, "ls-files", "-s", "-z", index=index, check=False)
    if result.returncode != 0:
        raise ManifestIncomplete("cannot read the index")
    return hashlib.sha256(result.stdout).hexdigest()


def _skip_git(rel: str) -> bool:
    parts = Path(rel).parts
    if len(parts) >= 4 and parts[1] == "worktrees" and parts[3] in {"logs", "objects"}:
        return True
    return (rel in (".git/objects", ".git/logs") or rel.endswith(".lock")
            or rel.startswith((".git/objects/", ".git/logs/")))


def _git_metadata(root: Path) -> list[tuple[Path, str]]:
    """Resolve alternate administrative roots without trusting pointer-file text."""
    marker = root / ".git"
    if not (marker.is_file() or marker.is_symlink() or (marker / "commondir").is_file()):
        return []
    from garuda.workspace.snapshot_proto import git

    try:
        result = git(root, "rev-parse", "--show-toplevel", "--absolute-git-dir", "--git-common-dir",
                     check=False)
        if result.returncode != 0:
            raise ValueError("Git cannot resolve the checkout")
        top, admin, common = os.fsdecode(result.stdout).splitlines()
        admin, common = ((root / path).resolve(strict=True) for path in (admin, common))
        if Path(top).resolve() != root or any(
                root.is_relative_to(path) or not path.is_dir() for path in (admin, common)):
            raise ValueError("Git metadata does not describe this workspace")
    except (OSError, RuntimeError, ValueError) as exc:
        raise ManifestIncomplete("cannot resolve checkout Git metadata") from exc
    roots = [(admin, ".git/admin")]
    if common != admin:
        roots.append((common, ".git/common"))
    return roots


def manifest(root, *, max_entries: int = MAX_ENTRIES) -> dict[str, tuple]:
    """``{relative path: facts}`` for every entry under ``root``.

    Outside ``.git`` every entry is described by type, mode, inode and change
    time (plus size and mtime for files, the target for symlinks). Inside
    ``.git`` only content counts — refs, ``HEAD``, config, hooks by digest and
    the index by its staged entries — and lock files, objects and reflogs are
    skipped (object writes surface through the refs).
    """
    root = Path(root).resolve()
    metadata = _git_metadata(root)
    metadata_paths = {path for path, _prefix in metadata}
    out: dict[str, tuple] = {}
    stack = [(root, root, "", False)]
    for path, prefix in metadata:
        out[prefix] = ("git-directory", str(path))
        stack.append((path, path, prefix, True))
    while stack:
        current, base, prefix, metadata_tree = stack.pop()
        try:
            entries = list(os.scandir(current))
        except OSError as exc:
            raise ManifestIncomplete(f"cannot list {current}: {exc}") from exc
        for entry in entries:
            if len(out) >= max_entries:
                raise ManifestIncomplete(f"more than {max_entries} entries")
            path = Path(entry.path)
            relative = path.relative_to(base).as_posix()
            if not metadata_tree and path in metadata_paths:
                continue
            rel = f"{prefix}/{relative}" if prefix else relative
            in_git = metadata_tree or rel == ".git" or rel.startswith(".git/")
            git_relative = ".git/" + relative if metadata_tree else rel
            if in_git and _skip_git(git_relative):
                continue
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError as exc:
                raise ManifestIncomplete(f"cannot stat {rel}: {exc}") from exc
            kind, mode = stat.S_IFMT(info.st_mode), stat.S_IMODE(info.st_mode)
            if stat.S_ISLNK(info.st_mode):
                try:
                    out[rel] = (kind, mode, os.readlink(path))
                except OSError as exc:
                    raise ManifestIncomplete(f"cannot read link {rel}") from exc
            elif stat.S_ISDIR(info.st_mode):
                out[rel] = (kind, mode) if in_git else (kind, mode, info.st_ino,
                                                         info.st_ctime_ns)
                stack.append((path, base, prefix, metadata_tree))
            elif in_git:
                parts = Path(git_relative).parts
                is_index = (git_relative == ".git/index" or (
                    len(parts) == 4 and parts[1] == "worktrees" and parts[3] == "index"))
                if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_METADATA_FILE_BYTES:
                    raise ManifestIncomplete(f"cannot observe Git metadata {rel}: unsupported file")
                try:
                    digest = _index_digest(root, path) if is_index else _digest(path)
                except OSError as exc:
                    raise ManifestIncomplete(f"cannot read Git metadata {rel}") from exc
                out[rel] = (kind, mode, digest)
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
