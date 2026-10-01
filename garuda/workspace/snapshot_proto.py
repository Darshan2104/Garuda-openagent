"""Prototype safe Git snapshots and integration publication — plan task A.5 (#154).

A spike with no session wiring. It proves, against real repositories, the Git
mechanics later tasks need (B.5 worktrees and integration, C.8b parallel review,
C.10 change detection, G.2 consult snapshots):

- **Stable, bounded snapshots that run nothing from the repository.** Every Git
  call runs with a sanitized environment: no inherited ``GIT_*`` variables, no
  system or global config, ``core.fsmonitor`` and ``core.hooksPath`` disabled,
  signing off. Files are listed NUL-delimited, read without following symlinks,
  hashed with ``hash-object --no-filters`` and written straight into a temporary
  index — never ``git add``, which can run clean filters. The source index and
  stash are never touched. The manifest is taken before and after; any change
  in between refuses the snapshot.
- **Explicit refusals** for what version 1 does not handle: non-Git workspaces,
  submodules, sparse checkouts, custom ``filter=`` attributes, symlinked parent
  directories, unsupported file types and anything over the size bounds.
- **Detached repositories with their own metadata** for consults: the snapshot's
  objects are written into a fresh ``git init`` (no alternates, no hardlinks, no
  linked-worktree links), so a child cannot reach the caller's refs or config.
- **One winner** when allocating a branch name, using a create-only ref update.
- **Integration preview** with ``merge-tree --write-tree``, which touches no
  checkout.
- **Publication by compare-and-swap** on ``refs/garuda/integration/<id>`` only.
  The destination branch, HEAD, index and checkout are never changed; the
  caller gets the manual ``merge --ff-only`` command.
- **Confined checks** run in Docker with the detached source mounted read-only,
  no network, no Docker socket and no host home; a check that changes the tree
  invalidates its evidence.
"""

from __future__ import annotations

import hashlib
import os
import shlex
import stat
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

ZERO_OID = "0" * 40
SANITIZED_CONFIG = (
    "-c", "core.fsmonitor=false",
    "-c", "core.hooksPath=/dev/null",
    "-c", "core.untrackedCache=false",
    "-c", "commit.gpgSign=false",
    "-c", "tag.gpgSign=false",
    "-c", "core.autocrlf=false",
)
IDENTITY = {
    "GIT_AUTHOR_NAME": "garuda",
    "GIT_AUTHOR_EMAIL": "garuda@localhost",
    "GIT_COMMITTER_NAME": "garuda",
    "GIT_COMMITTER_EMAIL": "garuda@localhost",
    "GIT_AUTHOR_DATE": "1970-01-01T00:00:00Z",
    "GIT_COMMITTER_DATE": "1970-01-01T00:00:00Z",
}


class SnapshotRefused(Exception):
    """The workspace cannot be snapshotted safely; ``code`` says why."""

    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code


@dataclass(frozen=True)
class Limits:
    max_files: int = 50_000
    max_file_bytes: int = 100 * 1024 * 1024
    max_total_bytes: int = 512 * 1024 * 1024


DEFAULT_LIMITS = Limits()


@dataclass(frozen=True)
class Entry:
    path: str
    mode: str  # 100644, 100755 or 120000; "" when deleted
    size: int
    mtime_ns: int
    inode: int


@dataclass(frozen=True)
class Snapshot:
    source_head: str | None
    tree: str
    commit: str
    manifest_digest: str
    deleted: tuple[str, ...] = ()
    object_repo: str = ""
    entries: tuple[Entry, ...] = field(default=(), repr=False)


def _env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", "/nonexistent"),
        "LANG": "C",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
        **IDENTITY,
    }
    env.update(extra or {})
    return env


def git(
    repo: str | Path,
    *args: str,
    git_dir: str | Path | None = None,
    index: str | Path | None = None,
    input: bytes | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess:
    """Run git with the sanitized environment and config overrides."""
    extra: dict[str, str] = {}
    if index is not None:
        extra["GIT_INDEX_FILE"] = str(index)
    cmd = ["git", *SANITIZED_CONFIG]
    if git_dir is not None:
        cmd += ["--git-dir", str(git_dir), "--work-tree", str(repo)]
    else:
        cmd += ["-C", str(repo)]
    result = subprocess.run(cmd + list(args), input=input, capture_output=True, env=_env(extra))
    if check and result.returncode != 0:
        raise SnapshotRefused(
            "git.failed", f"git {' '.join(args[:2])}: {result.stderr.decode(errors='replace').strip()}"
        )
    return result


def _out(result: subprocess.CompletedProcess) -> str:
    return result.stdout.decode().strip()


def _refuse_unsupported(repo: Path) -> None:
    inside = git(repo, "rev-parse", "--is-inside-work-tree", check=False)
    if inside.returncode != 0 or _out(inside) != "true":
        raise SnapshotRefused("snapshot.not_git", f"{repo} is not a Git work tree")
    top = Path(_out(git(repo, "rev-parse", "--show-toplevel"))).resolve()
    if top != repo.resolve():
        raise SnapshotRefused("snapshot.not_root", f"snapshot the repository root {top}")
    if (repo / ".gitmodules").exists() or "160000 " in _out(git(repo, "ls-files", "-s")):
        raise SnapshotRefused("snapshot.submodules", "submodules are not supported in v1")
    sparse = git(repo, "config", "--get", "core.sparseCheckout", check=False)
    if _out(sparse).lower() == "true":
        raise SnapshotRefused("snapshot.sparse", "sparse checkouts are not supported in v1")
    git_dir = Path(_out(git(repo, "rev-parse", "--absolute-git-dir")))
    attribute_files = [git_dir / "info" / "attributes"]
    listed = git(repo, "ls-files", "-z", "--cached", "--others", "--exclude-standard")
    attribute_files += [
        repo / p for p in listed.stdout.decode().split("\0") if p.endswith(".gitattributes")
    ]
    for path in attribute_files:
        try:
            if path.is_file() and "filter=" in path.read_text(errors="replace"):
                raise SnapshotRefused(
                    "snapshot.custom_filter", f"{path} declares a filter driver"
                )
        except OSError:
            continue


def _manifest(repo: Path, limits: Limits) -> list[Entry]:
    listed = git(repo, "ls-files", "-z", "--cached", "--others", "--exclude-standard")
    paths = sorted({p for p in listed.stdout.decode().split("\0") if p})
    if len(paths) > limits.max_files:
        raise SnapshotRefused("snapshot.too_large", f"{len(paths)} files exceed {limits.max_files}")
    entries: list[Entry] = []
    total = 0
    for rel in paths:
        parts = Path(rel).parts
        for depth in range(1, len(parts)):
            if (repo.joinpath(*parts[:depth])).is_symlink():
                raise SnapshotRefused(
                    "snapshot.symlinked_parent", f"{rel} sits under a symlinked directory"
                )
        try:
            info = os.lstat(repo / rel)
        except FileNotFoundError:
            entries.append(Entry(rel, "", 0, 0, 0))  # a deletion
            continue
        if stat.S_ISLNK(info.st_mode):
            mode = "120000"
        elif stat.S_ISREG(info.st_mode):
            mode = "100755" if info.st_mode & stat.S_IXUSR else "100644"
            if info.st_size > limits.max_file_bytes:
                raise SnapshotRefused("snapshot.too_large", f"{rel} exceeds {limits.max_file_bytes} bytes")
            total += info.st_size
            if total > limits.max_total_bytes:
                raise SnapshotRefused("snapshot.too_large", f"files exceed {limits.max_total_bytes} bytes")
        else:
            raise SnapshotRefused("snapshot.unsupported_type", f"{rel} is not a file or symlink")
        entries.append(Entry(rel, mode, info.st_size, info.st_mtime_ns, info.st_ino))
    return entries


def _digest(entries: list[Entry]) -> str:
    h = hashlib.sha256()
    for e in entries:
        h.update(f"{e.path}\0{e.mode}\0{e.size}\0{e.mtime_ns}\0{e.inode}\n".encode())
    return h.hexdigest()


def _read_no_follow(path: Path, mode: str) -> bytes:
    if mode == "120000":
        return os.readlink(path).encode()
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "rb") as handle:
        return handle.read()


def _hash_entries(repo: Path, entries: list[Entry], git_dir: Path) -> list[tuple[Entry, str]]:
    hashed = []
    for entry in entries:
        if not entry.mode:
            continue
        data = _read_no_follow(repo / entry.path, entry.mode)
        oid = _out(git(repo, "hash-object", "-w", "--no-filters", "--stdin", git_dir=git_dir, input=data))
        hashed.append((entry, oid))
    return hashed


def capture(
    repo: str | Path,
    *,
    object_repo: str | Path | None = None,
    limits: Limits = DEFAULT_LIMITS,
    message: str = "garuda snapshot",
) -> Snapshot:
    """Snapshot the work tree (tracked edits, deletions, untracked non-ignored files).

    Objects go into ``object_repo``'s object database when given (a detached
    repository), otherwise into the source repository's — which adds loose
    objects but changes no ref, index, stash or checkout.
    """
    repo = Path(repo).resolve()
    _refuse_unsupported(repo)
    if object_repo is None:
        git_dir = Path(_out(git(repo, "rev-parse", "--absolute-git-dir")))
    else:
        git_dir = Path(object_repo).resolve() / ".git"
    head = git(repo, "rev-parse", "--verify", "-q", "HEAD", check=False)
    source_head = _out(head) if head.returncode == 0 else None

    before = _manifest(repo, limits)
    hashed = _hash_entries(repo, before, git_dir)
    after = _manifest(repo, limits)
    if after != before:
        raise SnapshotRefused("snapshot.changed", "the work tree changed while it was captured")

    with tempfile.TemporaryDirectory(prefix="garuda-snapshot-index-") as scratch:
        index = Path(scratch) / "index"
        info = b"".join(f"{e.mode} {oid}\t{e.path}".encode() + b"\0" for e, oid in hashed)
        git(repo, "update-index", "-z", "--index-info", git_dir=git_dir, index=index, input=info)
        tree = _out(git(repo, "write-tree", git_dir=git_dir, index=index))
    parents = ["-p", source_head] if source_head and object_repo is None else []
    commit = _out(
        git(repo, "commit-tree", tree, *parents, "-m", f"{message}\n\nsource-head: {source_head}",
            git_dir=git_dir)
    )
    return Snapshot(
        source_head=source_head,
        tree=tree,
        commit=commit,
        manifest_digest=_digest(before),
        deleted=tuple(e.path for e in before if not e.mode),
        object_repo=str(git_dir.parent),
        entries=tuple(before),
    )


def detached_repository(repo: str | Path, dest: str | Path, *, limits: Limits = DEFAULT_LIMITS) -> Snapshot:
    """A fresh repository holding only the snapshot, checked out read-ready."""
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=False)
    git(dest, "init", "-q")
    snapshot = capture(repo, object_repo=dest, limits=limits)
    git(dest, "update-ref", "refs/garuda/snapshot", snapshot.commit)
    git(dest, "read-tree", "-u", "--reset", snapshot.commit)
    return snapshot


def allocate_branch(repo: str | Path, name: str, commit: str) -> bool:
    """Create ``refs/heads/garuda/<name>`` only if it does not exist. One winner."""
    result = git(repo, "update-ref", f"refs/heads/garuda/{name}", commit, ZERO_OID, check=False)
    return result.returncode == 0


@dataclass(frozen=True)
class Preview:
    clean: bool
    tree: str
    conflicts: tuple[str, ...]


def preview_integration(repo: str | Path, destination: str, source_commit: str) -> Preview:
    """Merge without a checkout: ``merge-tree --write-tree`` touches no work tree."""
    result = git(
        repo, "merge-tree", "--write-tree", "--name-only", "--no-messages",
        destination, source_commit, check=False,
    )
    lines = [line for line in result.stdout.decode().splitlines() if line]
    if result.returncode not in (0, 1) or not lines:
        raise SnapshotRefused("integration.preview_failed", result.stderr.decode(errors="replace"))
    return Preview(result.returncode == 0, lines[0], tuple(lines[1:]))


def publish_integration(repo: str | Path, session_id: str, commit: str, expected_old: str = ZERO_OID) -> str:
    """Point ``refs/garuda/integration/<session_id>`` at ``commit`` by compare-and-swap.

    Returns the command a person runs to apply it. Nothing else in the
    repository changes.
    """
    ref = f"refs/garuda/integration/{session_id}"
    result = git(repo, "update-ref", ref, commit, expected_old, check=False)
    if result.returncode != 0:
        raise SnapshotRefused("integration.ref_moved", f"{ref} is no longer at {expected_old}")
    return f"git -C {shlex.quote(str(repo))} merge --ff-only {commit}"


def run_confined_check(
    detached: str | Path, argv: list[str], *, image: str, timeout: float = 300
) -> tuple[int, bool]:
    """Run a check in Docker against a detached snapshot, mounted read-only.

    No network, no Docker socket, no host home, an unprivileged user and a
    disposable ``/tmp``. Returns ``(exit_code, tree_unchanged)``; a check that
    changed the tree does not count as evidence.
    """
    detached = Path(detached).resolve()
    before = _out(git(detached, "status", "--porcelain=v1", "-z", "--ignored"))
    cmd = [
        "docker", "run", "--rm", "--network", "none", "--read-only",
        "--tmpfs", "/tmp:rw,size=64m", "--user", "65534:65534",
        "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
        "-v", f"{detached}:/src:ro", "-w", "/src", image, *argv,
    ]
    result = subprocess.run(cmd, capture_output=True, timeout=timeout)
    after = _out(git(detached, "status", "--porcelain=v1", "-z", "--ignored"))
    return result.returncode, before == after
