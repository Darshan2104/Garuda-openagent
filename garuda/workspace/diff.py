"""Baseline and authoritative diff manager (P0.18, issue #28).

Git and the filesystem are the truth; ACP diff hints are advisory. A session
captures a baseline (commit, porcelain status, content fingerprints) up front;
the delta later reports each file as added/modified/deleted/renamed/untracked
with a `preexisting` flag separating dirt that predates the session from work
the session did. Large diffs are clipped inline but recoverable from disk, and
nothing here ever mutates the repository — only read-only git verbs appear.
"""

from __future__ import annotations

import hashlib
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

MAX_DIFF_CHARS = 20_000

#: The only git verbs this module may run. Anything else (checkout, clean,
#: reset, ...) would make the observer a participant.
_READONLY_VERBS = frozenset({"status", "diff", "rev-parse", "hash-object", "ls-files"})


def _git(path: str | Path, *args: str, timeout: float = 30.0) -> subprocess.CompletedProcess[str]:
    if not args or args[0] not in _READONLY_VERBS:
        raise DiffError(f"refusing non-read-only git invocation: {args!r}")
    return subprocess.run(
        ["git", "-C", str(path), *args],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


class DiffError(Exception):
    """Git is absent, the path is not a repository, or a read failed."""


@dataclass(frozen=True)
class Baseline:
    commit: str
    status_lines: tuple[str, ...]
    fingerprints: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "commit": self.commit,
            "status_lines": list(self.status_lines),
            "fingerprints": dict(self.fingerprints),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Baseline":
        if not isinstance(data, dict):
            raise DiffError("baseline must be a mapping")
        return cls(
            commit=str(data.get("commit", "")),
            status_lines=tuple(data.get("status_lines", [])),
            fingerprints=dict(data.get("fingerprints", {})),
        )


@dataclass(frozen=True)
class DeltaFile:
    path: str
    kind: str
    preexisting: bool = False


@dataclass(frozen=True)
class SessionDelta:
    files: tuple[DeltaFile, ...] = ()
    baseline_commit: str = ""

    @property
    def changed(self) -> tuple[str, ...]:
        return tuple(f.path for f in self.files if not f.preexisting)

    @property
    def preexisting(self) -> tuple[str, ...]:
        return tuple(f.path for f in self.files if f.preexisting)


def _porcelain_lines(path: str | Path) -> list[str]:
    result = _git(path, "status", "--porcelain=v1", "-uall")
    if result.returncode != 0:
        raise DiffError(f"git status failed: {result.stderr.strip()}")
    return [line for line in result.stdout.splitlines() if line.strip()]


def _porcelain_paths(lines: list[str]) -> dict[str, str]:
    """Map path -> XY status code from porcelain v1 lines."""
    out: dict[str, str] = {}
    for line in lines:
        code, _, rest = line[:2], line[2:3], line[3:]
        if " -> " in rest:
            rest = rest.split(" -> ", 1)[1]
        out[rest.strip().strip('"')] = code
    return out


def _hash_file(path: str | Path, rel: str) -> str:
    try:
        digest = hashlib.sha256()
        with open(Path(path) / rel, "rb") as handle:
            for chunk in iter(lambda: handle.read(65536), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return ""


def capture_baseline(path: str | Path) -> Baseline:
    """Record commit, status, and fingerprints. Empty baseline outside a repo."""
    commit_result = _git(path, "rev-parse", "HEAD")
    if commit_result.returncode != 0:
        return Baseline(commit="", status_lines=(), fingerprints={})
    try:
        lines = _porcelain_lines(path)
    except DiffError:
        lines = []
    fingerprints = {rel: _hash_file(path, rel) for rel in _porcelain_paths(lines)}
    return Baseline(
        commit=commit_result.stdout.strip(),
        status_lines=tuple(lines),
        fingerprints={k: v for k, v in fingerprints.items() if v},
    )


def session_delta(baseline: Baseline, path: str | Path) -> SessionDelta:
    """Diff the working tree against the baseline commit, flagging preexisting dirt."""
    try:
        current_lines = _porcelain_lines(path)
    except DiffError:
        return SessionDelta(baseline_commit=baseline.commit)
    current = _porcelain_paths(current_lines)
    base_paths = set(_porcelain_paths(list(baseline.status_lines)))
    base_prints = baseline.fingerprints

    renamed: dict[str, str] = {}
    letters: dict[str, str] = {}
    if baseline.commit:
        names = _git(path, "diff", "--name-status", "-M", baseline.commit, "--")
        if names.returncode == 0:
            for line in names.stdout.splitlines():
                parts = line.split("\t")
                if not parts:
                    continue
                if parts[0].startswith("R") and len(parts) == 3:
                    renamed[parts[2]] = parts[1]
                    letters[parts[2]] = "R"
                elif len(parts) == 2:
                    letters[parts[1]] = parts[0][:1]

    files: list[DeltaFile] = []
    rename_sources = set(renamed.values())
    for rel in sorted(set(current) | set(base_paths)):
        if rel in rename_sources:
            continue
        if rel in renamed:
            kind = "renamed"
        elif current.get(rel) == "??":
            kind = "untracked"
        elif letters.get(rel) == "A":
            kind = "added"
        elif letters.get(rel) == "D" or rel not in current:
            kind = "deleted"
        else:
            kind = "modified"
        preexisting = False
        if rel in base_paths:
            if rel not in current:
                preexisting = True
            elif _hash_file(path, rel) == base_prints.get(rel, "\x00"):
                preexisting = True
        files.append(DeltaFile(path=rel, kind=kind, preexisting=preexisting))
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


def load_session_delta(store, session_id: str, workspace: str | Path) -> SessionDelta:
    """Compute the delta from the baseline the session recorded at start.

    Handoff and verification consume the *recorded* baseline — never a fresh
    capture — so pre-existing dirt and agent work stay attributed exactly as
    the session saw them. Raises `DiffError` when no baseline was recorded.
    """
    try:
        recorded = store.load_meta(session_id).get("baseline") or {}
    except Exception as exc:
        raise DiffError(f"no readable session meta for {session_id}: {exc}") from exc
    if not recorded:
        raise DiffError(f"session {session_id} recorded no baseline")
    return session_delta(Baseline.from_dict(recorded), workspace)
