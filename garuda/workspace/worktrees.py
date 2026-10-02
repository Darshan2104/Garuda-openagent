"""Session worktrees and safe integration (plan task B.5, issue #157).

Promotes the A.5 snapshot spike (`snapshot_proto`) into the session-facing
service.

**Isolation.** A session works in one of:

- ``shared`` — the workspace itself, as today; a second editor is refused by
  the workspace lease;
- ``worktree`` — a linked Git worktree on its own ``garuda/<session-id>``
  branch, under ``<global home>/worktrees/``. The branch is created with a
  create-only ref update (one winner) and the worktree is added with hooks and
  filters disabled. Uncommitted changes in the source checkout are **not**
  carried over; the plan records that they existed and a fingerprint of them;
- ``auto`` — ``shared`` when nobody else is editing the workspace, otherwise a
  worktree.

A worktree is a separate place to edit, not confinement.

**Integration** (``garuda sessions merge``) never changes the user's checkout:

1. lock integration for the repository;
2. snapshot the session's worktree (including uncommitted edits) into a commit
   with hook-free, signing-free plumbing;
3. preview the merge into the destination with ``merge-tree --write-tree`` and
   refuse on conflicts;
4. write the merged tree into a scratch directory and run each required check
   in Docker with that directory mounted read-only, no network and no Docker
   socket. No check, or no Docker, refuses — a host check is never a
   substitute — and a check that changes the tree voids its evidence;
5. revalidate both refs, then publish only ``refs/garuda/integration/<id>`` by
   compare-and-swap and return the ``git merge --ff-only`` command to apply it.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from garuda.workspace import snapshot_proto as snap
from garuda.workspace.snapshot_proto import SnapshotRefused, git

ISOLATION_MODES = ("shared", "worktree", "auto")


class WorktreeError(Exception):
    """A worktree or integration request was refused; ``code`` says why."""

    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code


@dataclass(frozen=True)
class WorkspacePlan:
    path: str
    isolation: str  # shared | worktree
    source_repo: str
    branch: str | None = None
    source_head: str | None = None
    dirty_source: bool = False
    dirty_fingerprint: str | None = None

    def meta(self) -> dict:
        return {
            "isolation": self.isolation,
            "worktree": self.path if self.isolation == "worktree" else None,
            "branch": self.branch,
            "source_repo": self.source_repo,
            "source_head": self.source_head,
            "dirty_source": self.dirty_source,
            "dirty_fingerprint": self.dirty_fingerprint,
        }


def worktrees_root() -> Path:
    from garuda.config.agent_home import global_settings_path

    return global_settings_path().expanduser().parent / "worktrees"


def _repo_key(repo: Path) -> str:
    return hashlib.sha256(str(repo.resolve()).encode()).hexdigest()[:16]


def _someone_is_editing(repo: Path, session_id: str, lease_store) -> bool:
    from garuda.workspace.lease import LeaseStore

    store = lease_store or LeaseStore()
    for holder in store.holders_of(repo):
        if holder.mode != "mutating" or holder.session_id == session_id:
            continue
        if not holder.is_stale() or store._owner_state(holder) is not False:
            return True
    return False


def prepare_workspace(
    workspace: str | Path, session_id: str, isolation: str = "shared", *, lease_store=None
) -> WorkspacePlan:
    """Decide where a session works, creating its worktree when needed."""
    from garuda.core.sessions import validate_session_ref

    if isolation not in ISOLATION_MODES:
        raise WorktreeError("worktree.invalid_mode", f"isolation must be one of {ISOLATION_MODES}")
    repo = Path(workspace).resolve()
    if isolation == "auto":
        isolation = "worktree" if _someone_is_editing(repo, session_id, lease_store) else "shared"
    if isolation == "shared":
        return WorkspacePlan(path=str(repo), isolation="shared", source_repo=str(repo))

    validate_session_ref(session_id)
    try:
        return _create_worktree(repo, session_id)
    except SnapshotRefused as exc:
        raise WorktreeError(exc.code, str(exc)) from exc


def _create_worktree(repo: Path, session_id: str) -> WorkspacePlan:
    snap._refuse_unsupported(repo)
    head = git(repo, "rev-parse", "--verify", "-q", "HEAD", check=False)
    if head.returncode != 0:
        raise WorktreeError("worktree.no_commit", "worktree isolation needs at least one commit")
    source_head = head.stdout.decode().strip()
    status = git(repo, "status", "--porcelain=v1", "-z", "--untracked-files=all").stdout
    branch = f"garuda/{session_id}"
    if not snap.allocate_branch(repo, session_id, source_head):
        raise WorktreeError("worktree.branch_taken", f"branch {branch} already exists")
    target = worktrees_root() / _repo_key(repo) / session_id
    target.parent.mkdir(parents=True, exist_ok=True)
    added = git(repo, "worktree", "add", "--quiet", str(target), branch, check=False)
    if added.returncode != 0:
        git(repo, "update-ref", "-d", f"refs/heads/{branch}", source_head, check=False)
        raise WorktreeError("worktree.add_failed", added.stderr.decode(errors="replace").strip())
    return WorkspacePlan(
        path=str(target),
        isolation="worktree",
        source_repo=str(repo),
        branch=branch,
        source_head=source_head,
        dirty_source=bool(status),
        dirty_fingerprint=hashlib.sha256(status).hexdigest() if status else None,
    )


def discard_worktree(plan_meta: dict) -> None:
    """Remove a worktree and branch that were never used (a refused launch)."""
    repo, path, branch = (plan_meta.get(k) for k in ("source_repo", "worktree", "branch"))
    if not repo or not path:
        return
    git(repo, "worktree", "remove", "--force", path, check=False)
    if branch and plan_meta.get("source_head"):
        git(repo, "update-ref", "-d", f"refs/heads/{branch}", plan_meta["source_head"], check=False)


def remove_worktree(plan_meta: dict, *, force: bool = False) -> None:
    """Remove an owned worktree once what it holds is published.

    Safe when the worktree's current contents are exactly the session side of
    ``refs/garuda/integration/<id>``; anything else refuses unless ``force``.
    """
    repo, path = plan_meta.get("source_repo"), plan_meta.get("worktree")
    if not repo or not path:
        return
    if not force:
        session_id = Path(path).name
        published = git(repo, "rev-parse", "--verify", "-q",
                        f"refs/garuda/integration/{session_id}^2^{{tree}}", check=False)
        try:
            current = snap.capture(path).tree if Path(path).is_dir() else None
        except SnapshotRefused:
            current = None
        if published.returncode != 0 or current != published.stdout.decode().strip():
            raise WorktreeError(
                "worktree.unmerged",
                f"{path} holds work that was never published; merge it or pass --force",
            )
    git(repo, "worktree", "remove", "--force", path, check=False)


@dataclass
class IntegrationResult:
    commit: str
    destination: str
    apply_command: str
    checks: list[dict] = field(default_factory=list)


def _tree_digest(root: Path) -> str:
    h = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        if path.is_symlink():
            h.update(f"L {rel} {os.readlink(path)}\n".encode())
        elif path.is_file():
            h.update(f"F {rel} {oct(path.stat().st_mode & 0o777)}\n".encode())
            h.update(hashlib.sha256(path.read_bytes()).digest())
    return h.hexdigest()


def _materialize(repo: Path, tree: str, dest: Path) -> None:
    """Write ``tree``'s files into ``dest`` without touching the repo's index."""
    with tempfile.TemporaryDirectory(prefix="garuda-integration-index-") as scratch:
        index = Path(scratch) / "index"
        git(repo, "read-tree", tree, index=index)
        git(repo, "--work-tree", str(dest), "checkout-index", "--all", "--force", index=index)


def _run_check(directory: Path, command: str, image: str, timeout: float) -> dict:
    docker = shutil.which("docker")
    if docker is None:
        raise WorktreeError("integration.no_confined_checker", "Docker is not available")
    argv = [
        docker, "run", "--rm", "--network", "none", "--read-only",
        "--tmpfs", "/tmp:rw,size=256m", "--user", "65534:65534",
        "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
        "-v", f"{directory}:/src:ro", "-w", "/src", image, "sh", "-c", command,
    ]
    try:
        result = subprocess.run(argv, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"command": command, "exit_code": None, "passed": False, "timed_out": True}
    return {
        "command": command,
        "exit_code": result.returncode,
        "passed": result.returncode == 0,
        "output_tail": (result.stdout + result.stderr).decode(errors="replace")[-2000:],
    }


def merge_session(
    session_meta: dict,
    *,
    checks: list[str],
    image: str,
    destination: str | None = None,
    timeout: float = 600,
) -> IntegrationResult:
    """Prepare, check and publish a session's integration commit. See module docstring."""
    from garuda.runtime.strict_store import exclusive_lock

    repo = session_meta.get("source_repo")
    worktree = session_meta.get("worktree")
    session_id = session_meta.get("session_id")
    if not repo or not worktree or not session_id:
        raise WorktreeError("integration.not_a_worktree", "this session did not run in a worktree")
    if not checks:
        raise WorktreeError(
            "integration.no_trusted_check",
            "no check to run against the integration commit; pass --check COMMAND",
        )
    repo_path = Path(repo)
    lock_dir = worktrees_root() / _repo_key(repo_path) / ".integration"
    try:
        with exclusive_lock(lock_dir):
            return _merge_locked(repo_path, worktree, session_id, checks, image, destination, timeout)
    except SnapshotRefused as exc:
        raise WorktreeError(exc.code, str(exc)) from exc


def _destination_ref(repo: Path, destination: str | None) -> str:
    if destination is None:
        current = git(repo, "symbolic-ref", "-q", "HEAD", check=False)
        if current.returncode != 0:
            raise WorktreeError("integration.no_destination", "HEAD is detached; pass --into BRANCH")
        return current.stdout.decode().strip()
    valid = git(repo, "check-ref-format", "--branch", destination, check=False)
    if valid.returncode != 0 or destination.startswith("-"):
        raise WorktreeError("integration.no_destination", f"{destination!r} is not a branch name")
    return f"refs/heads/{destination}"


def _merge_locked(repo_path, worktree, session_id, checks, image, destination, timeout):
    """The steps of `merge_session` under its lock. See the module docstring."""
    ref = _destination_ref(repo_path, destination)
    destination = ref.removeprefix("refs/heads/")
    found = git(repo_path, "rev-parse", "--verify", "-q", f"{ref}^{{commit}}", check=False)
    if found.returncode != 0:
        raise WorktreeError("integration.no_destination", f"branch {destination} does not exist")
    dest_commit = found.stdout.decode().strip()
    source = snap.capture(worktree, message=f"garuda session {session_id}").commit
    preview = snap.preview_integration(repo_path, dest_commit, source)
    if not preview.clean:
        raise WorktreeError(
            "integration.conflicts",
            "the session conflicts with the destination: " + ", ".join(preview.conflicts),
        )
    commit = git(
        repo_path, "commit-tree", preview.tree, "-p", dest_commit, "-p", source,
        "-m", f"garuda: integrate session {session_id}",
    ).stdout.decode().strip()
    results = []
    with tempfile.TemporaryDirectory(prefix="garuda-integration-") as scratch:
        tree_dir = Path(scratch) / "src"
        tree_dir.mkdir()
        _materialize(repo_path, preview.tree, tree_dir)
        before = _tree_digest(tree_dir)
        for command in checks:
            outcome = _run_check(tree_dir, command, image, timeout)
            results.append(outcome)
            if not outcome["passed"]:
                raise WorktreeError("integration.check_failed", f"check failed: {command}")
        if _tree_digest(tree_dir) != before:
            raise WorktreeError("integration.check_changed_tree", "a check changed the tree")
    if git(repo_path, "rev-parse", "--verify", ref).stdout.decode().strip() != dest_commit:
        raise WorktreeError("integration.destination_moved", f"{destination} moved during checks")
    apply = snap.publish_integration(repo_path, session_id, commit)
    return IntegrationResult(
        commit=commit,
        destination=destination,
        apply_command=apply,
        checks=results,
    )
