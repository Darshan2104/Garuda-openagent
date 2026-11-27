"""Checkpoints of a worktree flow's workspace on private Git refs.

A flow that runs in its own worktree (``isolation: worktree``) records, at the
end of every step attempt, a commit of the worktree's full state: tracked
edits, deletions and untracked files that ``.gitignore`` does not exclude. The
commit is built with a temporary index (``GIT_INDEX_FILE``, ``git add -A``,
``write-tree``, ``commit-tree``) and published only as
``refs/garuda/<flow_session>/<n>`` with a create-only ``update-ref``. The
user's branch, index and HEAD, and the worktree's own index and HEAD, are
never changed.

Each checkpoint is recorded in ``<flow dir>/checkpoints.jsonl`` (never in the
flow journal, which keeps exactly two entries per step attempt). Disk use is
bounded: an unchanged tree records nothing new, and past :data:`MAX_CHECKPOINTS`
the oldest are pruned, never the first or the last.

:func:`checkpoint` is the one entry point; a caller inside a step (a turn
loop) may call it at each turn end. It is a no-op (``None``) outside a Garuda
worktree, so a shared-checkout flow never gets refs or records.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path

from garuda.workspace import snapshot_proto as snap

CHECKPOINTS = "checkpoints.jsonl"
#: Checkpoints kept per flow; the first and the last are never pruned.
MAX_CHECKPOINTS = 64
REF_ROOT = "refs/garuda"


class CheckpointError(Exception):
    """A checkpoint, rollback or prune was refused; ``code`` says why."""

    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


def _git(repo, *args, index=None, check=True, dated=False) -> subprocess.CompletedProcess:
    """Git with the snapshot module's sanitized environment and config.

    No system or global config, hooks or fsmonitor; ``dated`` gives the
    commit the real time instead of the fixed snapshot identity date.
    """
    extra = {"GIT_INDEX_FILE": str(index)} if index is not None else {}
    if dated:
        stamp = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        extra.update(GIT_AUTHOR_DATE=stamp, GIT_COMMITTER_DATE=stamp)
    result = subprocess.run(["git", *snap.SANITIZED_CONFIG, "-C", str(repo), *args],
                            capture_output=True, env=snap._env(extra))
    if check and result.returncode != 0:
        raise CheckpointError("checkpoint.git_failed",
                              f"git {' '.join(args[:2])}: "
                              f"{result.stderr.decode(errors='replace').strip()}")
    return result


def _out(result) -> str:
    return result.stdout.decode().strip()


def is_flow_worktree(workspace) -> bool:
    """Whether ``workspace`` is a Garuda-owned linked worktree."""
    from garuda.workspace.worktrees import worktrees_root

    try:
        path = Path(workspace).resolve(strict=True)
        root = worktrees_root().resolve()
    except OSError:
        return False
    return path != root and root in path.parents and (path / ".git").is_file()


def ref_name(flow_session: str, n: int) -> str:
    return f"{REF_ROOT}/{flow_session}/{n}"


# -- the per-flow record ----------------------------------------------------------------


@contextmanager
def _locked(flow_dir: Path):
    import fcntl

    fd = os.open(flow_dir / "checkpoints.lock",
                 os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _events(flow_dir: Path) -> list[dict]:
    path = Path(flow_dir) / CHECKPOINTS
    if not path.is_file():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue  # a torn append
        if isinstance(row, dict):
            out.append(row)
    return out


def _append(flow_dir: Path, record: dict) -> None:
    fd = os.open(Path(flow_dir) / CHECKPOINTS,
                 os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        os.write(fd, (json.dumps(record, sort_keys=True) + "\n").encode())
        os.fsync(fd)
    finally:
        os.close(fd)


def listing(flow_dir) -> list[dict]:
    """Recorded checkpoints in order, each with ``pruned`` true once its ref is gone.

    Rollbacks are folded in as ``rolled_back_to`` on the checkpoint taken
    just before each rollback.
    """
    rows: dict[int, dict] = {}
    for event in _events(Path(flow_dir)):
        kind = event.get("event", "checkpoint")
        n = event.get("n")
        if kind == "checkpoint" and type(n) is int:
            rows[n] = {**event, "pruned": False}
        elif kind == "pruned" and type(n) is int and n in rows:
            rows[n]["pruned"] = True
        elif kind == "rollback" and type(event.get("before")) is int and event["before"] in rows:
            rows[event["before"]]["rolled_back_to"] = event.get("to")
    return [rows[n] for n in sorted(rows)]


# -- checkpoint ----------------------------------------------------------------------------


def _tree_of_worktree(workspace: Path, scratch: Path) -> tuple[str, Path]:
    """The worktree's full state as a tree, through a temporary index.

    The worktree's own index is copied, never written, so ``git add -A``
    reuses its stat cache; ``.gitignore`` decides what an untracked file is.
    """
    index = scratch / "index"
    real = Path(_out(_git(workspace, "rev-parse", "--git-path", "index")))
    real = real if real.is_absolute() else workspace / real
    if real.is_file():
        # copy2 keeps the index's mtime, so Git's racy-entry check still
        # rehashes a file written in the same second the index was.
        shutil.copy2(real, index)
    else:
        _git(workspace, "read-tree", "--empty", index=index)
    _git(workspace, "add", "-A", "--", ".", index=index)
    return _out(_git(workspace, "write-tree", index=index)), index


def checkpoint(flow_dir, workspace, label: str, *, step: str | None = None,
               attempt: int | None = None, limit: int = MAX_CHECKPOINTS) -> str | None:
    """Record the worktree's state on a private ref; return that ref.

    Returns the latest checkpoint's ref without a new one when the tree is
    unchanged since it, and ``None`` when ``workspace`` is not a Garuda
    worktree (shared isolation has no checkpoints). Raises
    :class:`CheckpointError` when Git refuses.
    """
    flow_dir = Path(flow_dir)
    if not is_flow_worktree(workspace) or not flow_dir.is_dir():
        return None
    workspace = Path(workspace).resolve()
    flow_session = flow_dir.parent.name
    from garuda.core.sessions import validate_session_ref

    validate_session_ref(flow_session)
    with _locked(flow_dir), tempfile.TemporaryDirectory(prefix="garuda-checkpoint-") as scratch:
        tree, _index = _tree_of_worktree(workspace, Path(scratch))
        rows = listing(flow_dir)
        last = rows[-1] if rows else None
        if last is not None and last.get("tree") == tree:
            return last["ref"]
        # Every checkpoint's parent is the worktree's HEAD, never the previous
        # checkpoint, so a pruned checkpoint's objects become unreachable.
        head = _out(_git(workspace, "rev-parse", "--verify", "-q", "HEAD"))
        n = (last["n"] + 1) if last is not None else 1
        message = f"garuda checkpoint {n}: {label}\n\nflow: {flow_session}\n"
        if step is not None:
            message += f"step: {step}\nattempt: {attempt}\n"
        commit = _out(_git(workspace, "commit-tree", tree, "-p", head, "-m", message,
                           dated=True))
        ref = ref_name(flow_session, n)
        _git(workspace, "update-ref", ref, commit, snap.ZERO_OID)
        _append(flow_dir, {"event": "checkpoint", "n": n, "ref": ref, "commit": commit,
                           "tree": tree, "step": step, "attempt": attempt,
                           "label": str(label)[:200], "at": _now()})
        kept = [row for row in rows if not row["pruned"]]
        kept.append({"n": n, "ref": ref, "commit": commit})
        _prune_excess(flow_dir, workspace, kept, limit)
        return ref


def _delete_ref(workspace, ref: str, commit: str | None) -> None:
    args = ["update-ref", "-d", ref] + ([commit] if commit else [])
    _git(workspace, *args, check=False)


def _prune_excess(flow_dir: Path, workspace: Path, kept: list[dict], limit: int) -> None:
    """Oldest first, past ``limit``, never the first or the last checkpoint."""
    limit = max(2, int(limit))
    excess = len(kept) - limit
    for row in kept[1:-1][:max(0, excess)]:
        _delete_ref(workspace, row["ref"], row.get("commit"))
        _append(flow_dir, {"event": "pruned", "n": row["n"], "at": _now()})


# -- rollback ------------------------------------------------------------------------------


def rollback(flow_dir, workspace, n: int) -> dict:
    """Restore the worktree's files to checkpoint ``n``.

    The current state is checkpointed first, so a rollback is itself
    reversible. Files the checkpoint does not hold (tracked or untracked) are
    removed; ignored files are left alone. The worktree's HEAD, branch and
    index are not changed. The caller holds the flow's workspace lease.
    """
    flow_dir = Path(flow_dir)
    if not is_flow_worktree(workspace):
        raise CheckpointError("flow.rollback_shared",
                              "rollback needs a flow that ran in its own worktree")
    rows = {row["n"]: row for row in listing(flow_dir)}
    target = rows.get(n)
    if target is None:
        raise CheckpointError("flow.checkpoint_unknown", f"this flow has no checkpoint {n}")
    if target["pruned"]:
        raise CheckpointError("flow.checkpoint_pruned", f"checkpoint {n} was pruned")
    workspace = Path(workspace).resolve()
    found = _git(workspace, "rev-parse", "--verify", "-q", f"{target['ref']}^{{commit}}",
                 check=False)
    if found.returncode != 0 or _out(found) != target["commit"]:
        raise CheckpointError("flow.checkpoint_changed",
                              f"{target['ref']} no longer points at checkpoint {n}'s commit")
    # Not pruned now, so the target survives; the next checkpoint prunes as usual.
    before = checkpoint(flow_dir, workspace, f"before rollback to {n}", limit=10**9)
    before_n = next(row["n"] for row in reversed(listing(flow_dir)) if row["ref"] == before)
    with _locked(flow_dir), tempfile.TemporaryDirectory(prefix="garuda-rollback-") as scratch:
        current, index = _tree_of_worktree(workspace, Path(scratch))
        # A two-tree switch on the scratch index updates exactly the paths
        # that differ, removing ones the checkpoint does not hold.
        _git(workspace, "read-tree", "--reset", "-u", current, target["tree"], index=index)
        restored = _out(_git(workspace, "write-tree", index=index))
        if restored != target["tree"]:
            raise CheckpointError("flow.rollback_incomplete",
                                  "the worktree does not match the checkpoint after restore")
        record = {"event": "rollback", "to": n, "before": before_n, "at": _now()}
        _append(flow_dir, record)
    return record


# -- pruning refs ----------------------------------------------------------------------------


def _namespaces(repo) -> dict[str, list[tuple[str, int]]]:
    """``{flow_session: [(ref, committer time), ...]}`` for checkpoint refs only."""
    from garuda.core.sessions import validate_session_ref

    listed = _git(repo, "for-each-ref", "--format=%(refname) %(committerdate:unix)",
                  f"{REF_ROOT}/", check=False)
    out: dict[str, list[tuple[str, int]]] = {}
    for line in listed.stdout.decode().splitlines():
        ref, _, when = line.partition(" ")
        parts = ref.split("/")
        if len(parts) != 4 or not parts[3].isdigit() or parts[2] == "integration":
            continue
        try:
            validate_session_ref(parts[2])
        except ValueError:
            continue
        out.setdefault(parts[2], []).append((ref, int(when) if when.isdigit() else 0))
    return out


def prune_refs(repo, flow_session: str) -> int:
    """Delete every checkpoint ref of one flow; returns how many."""
    refs = _namespaces(repo).get(flow_session, [])
    for ref, _when in refs:
        _delete_ref(repo, ref, None)
    return len(refs)


def prune_expired(repo, keep_days: int | None, *, now: float | None = None) -> list[str]:
    """Delete the checkpoint refs of flows whose newest checkpoint is older than ``keep_days``.

    A flow whose worktree is still leased (running) keeps its refs.
    """
    import time

    from garuda.workspace.lease import LeaseStore
    from garuda.workspace.worktrees import _repo_key, worktrees_root

    if not keep_days:
        return []
    horizon = (time.time() if now is None else now) - keep_days * 86400
    home = worktrees_root() / _repo_key(Path(repo))
    leases = LeaseStore()
    pruned = []
    for flow_session, refs in _namespaces(repo).items():
        if max(when for _ref, when in refs) >= horizon:
            continue
        worktree = home / flow_session
        if worktree.exists() and leases.holders_of(worktree):
            continue
        prune_refs(repo, flow_session)
        pruned.append(flow_session)
    return pruned


def record_failure(flow_dir, step: str | None, attempt: int | None, exc: BaseException) -> None:
    """Note a refused checkpoint; best effort, it never raises."""
    try:
        _append(Path(flow_dir), {"event": "failed", "step": step, "attempt": attempt,
                                 "reason": str(exc)[:500], "at": _now()})
    except OSError:
        pass
