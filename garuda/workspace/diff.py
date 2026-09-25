"""Baseline and authoritative diff mechanics (P0.18, issue #28).

Git and the filesystem are the truth; ACP diff hints are advisory. A baseline
records the commit, porcelain status, and content fingerprints of a workspace;
the delta later reports each file as added/modified/deleted/renamed/untracked
with a `preexisting` flag separating dirt that predates the baseline from work
done since. Large diffs are clipped inline but recoverable from disk, and
nothing here ever mutates the repository — only read-only git verbs appear,
with optional index locks disabled.

Session persistence (which baseline a session may use, and which workspace
kinds can be attributed at all) lives in `garuda.workspace.evidence`.
"""

from __future__ import annotations

import hashlib
import os
import stat
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

MAX_DIFF_CHARS = 20_000

#: A baseline captured from a Git work tree: deltas are attributable.
BASELINE_CAPTURED = "captured"
#: The workspace is not a Git work tree; there is no authoritative delta.
BASELINE_UNSUPPORTED_NONREPO = "unsupported_nonrepo"
#: The mutated tree is not the host path (a remote daemon's filesystem).
BASELINE_UNSUPPORTED_NONLOCAL = "unsupported_nonlocal"

_BASELINE_RECORD_STATES = frozenset({BASELINE_CAPTURED, BASELINE_UNSUPPORTED_NONREPO})

#: The only git verbs this module may run. Anything else (checkout, clean,
#: reset, ...) would make the observer a participant.
_READONLY_VERBS = frozenset({"status", "diff", "rev-parse", "hash-object", "ls-files"})


class DiffError(Exception):
    """Git is absent, the path is not a repository, or a read failed."""


class BaselineError(DiffError):
    """The session cannot make the immutable workspace-evidence claim."""


def _git(path: str | Path, *args: str, timeout: float = 30.0) -> subprocess.CompletedProcess[str]:
    if not args or args[0] not in _READONLY_VERBS:
        raise DiffError(f"refusing non-read-only git invocation: {args!r}")
    # A stable locale keeps the "not a git repository" check meaningful, and
    # GIT_OPTIONAL_LOCKS=0 stops `git status` from refreshing the index — an
    # observer must not write to the repository it observes.
    env = {**os.environ, "LC_ALL": "C", "GIT_OPTIONAL_LOCKS": "0"}
    try:
        return subprocess.run(
            ["git", "-C", str(path), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="surrogateescape",
            timeout=timeout,
            env=env,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise DiffError(f"git could not run: {exc}") from exc


@dataclass(frozen=True)
class Baseline:
    commit: str
    #: `"XY path"` entries, paths relative to the workspace (not the repo root).
    status_lines: tuple[str, ...]
    fingerprints: dict[str, str] = field(default_factory=dict)
    state: str = BASELINE_CAPTURED
    #: `git rev-parse --show-prefix` of the workspace inside its repository.
    prefix: str = ""

    @property
    def attributable(self) -> bool:
        return self.state == BASELINE_CAPTURED

    def to_dict(self) -> dict:
        return {
            "state": self.state,
            "commit": self.commit,
            "prefix": self.prefix,
            "status_lines": list(self.status_lines),
            "fingerprints": dict(self.fingerprints),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Baseline":
        if not isinstance(data, dict):
            raise DiffError("baseline must be a mapping")
        state = data.get("state", BASELINE_CAPTURED)
        if state not in _BASELINE_RECORD_STATES:
            raise DiffError(f"baseline.state {state!r} is not a recorded baseline state")
        prefix = data.get("prefix", "")
        if not isinstance(prefix, str):
            raise DiffError("baseline.prefix must be a string")
        status_lines = data.get("status_lines", [])
        fingerprints = data.get("fingerprints", {})
        if not isinstance(status_lines, list) or any(not isinstance(line, str) for line in status_lines):
            raise DiffError("baseline.status_lines must be a list of strings")
        if not isinstance(fingerprints, dict) or any(
            not isinstance(path, str) or not isinstance(digest, str)
            for path, digest in fingerprints.items()
        ):
            raise DiffError("baseline.fingerprints must be a string mapping")
        return cls(
            commit=str(data.get("commit", "")),
            status_lines=tuple(status_lines),
            fingerprints=dict(fingerprints),
            state=state,
            prefix=prefix,
        )


@dataclass(frozen=True)
class DeltaFile:
    path: str
    kind: str
    preexisting: bool = False
    #: This path was already dirty when the session started, but changed again
    #: during this session.  It is session work, not a pre-existing-only row.
    preexisting_at_start: bool = False


@dataclass(frozen=True)
class SessionDelta:
    files: tuple[DeltaFile, ...] = ()
    baseline_commit: str = ""
    #: `captured` for a real delta; otherwise the reason there is none. An
    #: unattributable delta has no files, and that emptiness is *not* a claim
    #: that nothing changed.
    attribution: str = BASELINE_CAPTURED

    @property
    def attributable(self) -> bool:
        return self.attribution == BASELINE_CAPTURED

    @property
    def changed(self) -> tuple[str, ...]:
        return tuple(f.path for f in self.files if not f.preexisting)

    @property
    def preexisting(self) -> tuple[str, ...]:
        return tuple(f.path for f in self.files if f.preexisting)

    def to_evidence(self, *, limit: int | None = None) -> dict:
        """The persisted/attached shape. Unattributable deltas carry no file
        lists at all, so no reader can mistake them for "no changes"."""
        if not self.attributable:
            return {"attribution": self.attribution}
        changed = list(self.changed)
        preexisting = list(self.preexisting)
        if limit is not None:
            changed, preexisting = changed[:limit], preexisting[:limit]
        return {
            "attribution": self.attribution,
            "baseline_commit": self.baseline_commit,
            "changed": changed,
            "preexisting": preexisting,
        }


def _split_z(stdout: str) -> list[str]:
    tokens = stdout.split("\0")
    if tokens and tokens[-1] == "":
        tokens.pop()
    return tokens


def _workspace_relative(repo_path: str, prefix: str) -> str:
    """Porcelain paths are repo-root relative; the delta speaks workspace paths."""
    if prefix and not repo_path.startswith(prefix):
        raise DiffError(f"git reported {repo_path!r} outside workspace prefix {prefix!r}")
    return repo_path[len(prefix):]


def _repo_prefix(path: str | Path) -> str:
    result = _git(path, "rev-parse", "--show-prefix")
    if result.returncode != 0:
        raise DiffError(f"could not read workspace prefix: {result.stderr.strip()}")
    return result.stdout.rstrip("\n")


def _status_entries(path: str | Path, prefix: str) -> dict[str, str]:
    """Map workspace-relative path -> XY code, NUL-delimited so no quoting or
    C-escaping applies, and restricted to the workspace subtree."""
    result = _git(
        path, "status", "--porcelain=v1", "-z", "-uall", "--no-renames", "--", "."
    )
    if result.returncode != 0:
        raise DiffError(f"git status failed: {result.stderr.strip()}")
    out: dict[str, str] = {}
    tokens = _split_z(result.stdout)
    index = 0
    while index < len(tokens):
        entry = tokens[index]
        index += 1
        if len(entry) < 4 or entry[2] != " ":
            raise DiffError(f"unparseable porcelain entry: {entry!r}")
        code = entry[:2]
        if code[0] in "RC":
            # Defensive: with --no-renames there is no source token, but a
            # future flag change must not shift every later entry by one.
            index += 1
        out[_workspace_relative(entry[3:], prefix)] = code
    return out


def _entries_from_lines(lines: tuple[str, ...] | list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in lines:
        if len(line) < 4 or line[2] != " ":
            raise DiffError(f"unparseable baseline status entry: {line!r}")
        out[line[3:]] = line[:2]
    return out


def _name_status(path: str | Path, commit: str) -> tuple[dict[str, str], dict[str, str]]:
    """(renamed dst -> src, path -> status letter) against ``commit``.

    `--relative` keeps paths workspace-relative and inside the workspace.
    """
    result = _git(
        path, "diff", "--name-status", "-z", "-M", "--relative", commit, "--", "."
    )
    if result.returncode != 0:
        raise DiffError(f"git diff --name-status failed: {result.stderr.strip()}")
    renamed: dict[str, str] = {}
    letters: dict[str, str] = {}
    tokens = _split_z(result.stdout)
    index = 0
    while index < len(tokens):
        status = tokens[index]
        index += 1
        if not status:
            raise DiffError("empty name-status token")
        if status[0] in "RC":
            if index + 2 > len(tokens):
                raise DiffError(f"truncated name-status rename entry: {status!r}")
            source, destination = tokens[index], tokens[index + 1]
            index += 2
            if status[0] == "R":
                renamed[destination] = source
                letters[destination] = "R"
            else:
                letters[destination] = "A"
            continue
        if index >= len(tokens):
            raise DiffError(f"truncated name-status entry: {status!r}")
        letters[tokens[index]] = status[0]
        index += 1
    return renamed, letters


def _fingerprint(root: str | Path, rel: str) -> str:
    """Content identity without following links or opening non-regular files.

    Symlinks hash their link text (as git does); FIFOs, devices, sockets and
    directories get a stable type marker and are never opened, so a link to
    /dev/zero or a FIFO cannot hang the delta. "" means absent.
    """
    full = os.path.join(os.fspath(root), rel)
    try:
        info = os.lstat(full)
    except (FileNotFoundError, NotADirectoryError):
        return ""
    except OSError as exc:
        raise DiffError(f"could not stat {rel!r}: {exc}") from exc
    if stat.S_ISLNK(info.st_mode):
        try:
            target = os.readlink(os.fsencode(full))
        except OSError as exc:
            raise DiffError(f"could not read link {rel!r}: {exc}") from exc
        return "symlink:" + hashlib.sha256(target).hexdigest()
    if not stat.S_ISREG(info.st_mode):
        return f"special:{_special_kind(info.st_mode)}"
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(full, flags)
    except FileNotFoundError:
        return ""
    except OSError as exc:
        raise DiffError(f"could not open {rel!r}: {exc}") from exc
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode):
            # Swapped between lstat and open; still never read it.
            return f"special:{_special_kind(opened.st_mode)}"
        digest = hashlib.sha256()
        while chunk := os.read(fd, 65536):
            digest.update(chunk)
    except OSError as exc:
        raise DiffError(f"could not read {rel!r}: {exc}") from exc
    finally:
        os.close(fd)
    return "sha256:" + digest.hexdigest()


def _special_kind(mode: int) -> str:
    if stat.S_ISDIR(mode):
        return "directory"
    if stat.S_ISFIFO(mode):
        return "fifo"
    if stat.S_ISCHR(mode):
        return "char-device"
    if stat.S_ISBLK(mode):
        return "block-device"
    if stat.S_ISSOCK(mode):
        return "socket"
    return "other"


def _head_commit(path: str | Path) -> str:
    """HEAD's object id, or "" for an unborn branch.

    Porcelain v2's branch header states "(initial)" explicitly, so an unborn
    HEAD is distinguishable from a failed read (which raises).
    """
    result = _git(path, "status", "--porcelain=v2", "--branch", "-z", "--untracked-files=no")
    if result.returncode != 0:
        raise DiffError(f"could not read baseline commit: {result.stderr.strip()}")
    for token in _split_z(result.stdout):
        if token.startswith("# branch.oid "):
            oid = token[len("# branch.oid "):].strip()
            return "" if oid == "(initial)" else oid
    raise DiffError("git status reported no branch.oid header")


def capture_baseline(path: str | Path) -> Baseline:
    """Record commit, status, and fingerprints.

    Outside a Git work tree the baseline is explicitly `unsupported_nonrepo`;
    any other failure to read the repository raises `DiffError`.
    """
    repository = _git(path, "rev-parse", "--is-inside-work-tree")
    if repository.returncode != 0:
        if "not a git repository" not in repository.stderr.lower():
            raise DiffError(f"could not determine repository state: {repository.stderr.strip()}")
        return Baseline(commit="", status_lines=(), state=BASELINE_UNSUPPORTED_NONREPO)
    if repository.stdout.strip() != "true":
        # Inside a .git directory or a bare repository: not a work tree we can
        # attribute, and not the plain "no repository" case either.
        raise DiffError("workspace is inside a git directory, not a work tree")
    commit = _head_commit(path)
    prefix = _repo_prefix(path)
    entries = _status_entries(path, prefix)
    # Keep the empty fingerprint too.  It represents a path that was already
    # deleted at baseline; dropping it made an unchanged deletion indistinguishable
    # from a deletion performed by this session.
    fingerprints = {rel: _fingerprint(path, rel) for rel in entries}
    return Baseline(
        commit=commit,
        status_lines=tuple(f"{code} {rel}" for rel, code in sorted(entries.items())),
        fingerprints=fingerprints,
        state=BASELINE_CAPTURED,
        prefix=prefix,
    )


def session_delta(baseline: Baseline, path: str | Path) -> SessionDelta:
    """Diff the working tree against the baseline, flagging preexisting dirt.

    A non-repo baseline yields an explicit unattributable delta. For a
    repository baseline every git failure raises `DiffError`: a failed read is
    never reported as "nothing changed".
    """
    if baseline.state == BASELINE_UNSUPPORTED_NONREPO:
        return SessionDelta(attribution=BASELINE_UNSUPPORTED_NONREPO)
    if baseline.state != BASELINE_CAPTURED:
        raise DiffError(f"baseline state {baseline.state!r} cannot produce a delta")
    prefix = _repo_prefix(path)
    if prefix != baseline.prefix:
        raise DiffError(
            f"workspace prefix changed since baseline ({baseline.prefix!r} -> {prefix!r})"
        )
    current = _status_entries(path, prefix)
    base_paths = set(_entries_from_lines(baseline.status_lines))
    base_prints = baseline.fingerprints

    renamed: dict[str, str] = {}
    letters: dict[str, str] = {}
    if baseline.commit:
        renamed, letters = _name_status(path, baseline.commit)

    files: list[DeltaFile] = []
    rename_sources = set(renamed.values())
    for rel in sorted(set(current) | base_paths):
        if rel in rename_sources:
            continue
        current_fingerprint = _fingerprint(path, rel)
        inherited = rel in base_paths
        code = current.get(rel, "  ")
        if rel in renamed:
            kind = "renamed"
        elif code == "??":
            kind = "untracked"
        elif letters.get(rel) == "A" or code[0] == "A":
            kind = "added"
        elif letters.get(rel) == "D" or not current_fingerprint:
            kind = "deleted"
        elif inherited and rel not in current:
            # The dirty baseline path is clean now. It was restored to HEAD,
            # which is a session change, not a deleted pre-existing row.
            kind = "restored"
        else:
            kind = "modified"
        preexisting = inherited and current_fingerprint == base_prints.get(rel, "\x00")
        files.append(
            DeltaFile(
                path=rel,
                kind=kind,
                preexisting=preexisting,
                preexisting_at_start=inherited and not preexisting,
            )
        )
    return SessionDelta(files=tuple(files), baseline_commit=baseline.commit)


def diff_text(path: str | Path, *, limit: int = MAX_DIFF_CHARS) -> tuple[str, str]:
    """Bounded `git diff HEAD` plus the full text. Callers show the first,
    persist the second — a large diff stays recoverable from disk."""
    result = _git(path, "diff", "HEAD", "--", ".")
    full = result.stdout if result.returncode == 0 else ""
    if len(full) <= limit:
        return full, full
    head = limit * 2 // 3
    clipped = len(full) - limit
    return (
        full[:head] + f"\n[… diff clipped {clipped} chars; see full text]\n",
        full,
    )


@dataclass(frozen=True)
class Reconciliation:
    confirmed: tuple[str, ...]
    disagreed: tuple[str, ...]


def reconcile(acp_hints: list[str], delta: SessionDelta) -> Reconciliation:
    """Check ACP diff hints against filesystem truth. Hints never add files —
    a claimed path absent from the delta is a disagreement, not a change."""
    on_disk = {f.path for f in delta.files}
    confirmed = tuple(h for h in acp_hints if h in on_disk)
    disagreed = tuple(h for h in acp_hints if h not in on_disk)
    return Reconciliation(confirmed=confirmed, disagreed=disagreed)
