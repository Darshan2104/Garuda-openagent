"""Bounded local editing-worker inventory and conservative repository retirement (#384).

Receipts remain as audit records. Retiring a repository never releases leases or
capacity: missing descendant-settlement evidence requires supervised recovery.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import tempfile
import uuid
from pathlib import Path

from garuda.runtime.capacity import CapacityError
from garuda.runtime.private_records import open_directory, read_record
from garuda.runtime.strict_store import StorageError, exclusive_lock, write_locked_document
from garuda.workspace import snapshot_proto as snap
from garuda.workspace.lease import LeaseError, LeaseStore
from garuda.workspace.worktrees import WorktreeError, worktrees_root

MAX_WORKERS = 128
_TERMINAL = {"failed", "refused", "integrated", "uncertain"}


def editing_workers_root() -> Path:
    return worktrees_root() / "editing-workers"


def repository_identity(workspace: Path) -> dict:
    info = workspace.stat(follow_symlinks=False)
    if not stat.S_ISDIR(info.st_mode):
        raise WorktreeError("worker.uncertain", "worker repository is not a directory")
    return {"device": info.st_dev, "inode": info.st_ino}


class EditingWorkerRepositories:
    """Read without locks; mutate only one receipt-bound, settled local repository."""

    def __init__(self, root: Path | None = None):
        self.root = Path(os.path.abspath(root if root is not None else editing_workers_root()))

    def _read(self, directory: Path) -> dict:
        fd = open_directory(directory)
        try:
            receipt = read_record(fd, "receipt.json")
        finally:
            os.close(fd)
        if not receipt:
            raise WorktreeError("worker.uncertain", "worker receipt is missing")
        candidate = receipt.get("candidate_id")
        try:
            if str(uuid.UUID(candidate)) != candidate:
                raise ValueError("noncanonical worker id")
        except (TypeError, ValueError, AttributeError) as exc:
            raise WorktreeError("worker.uncertain", "worker identity is invalid") from exc
        if (not isinstance(receipt.get("parent_session_id"), str)
                or not receipt["parent_session_id"] or not isinstance(receipt.get("state"), str)
                or receipt.get("workspace") != str(directory / "workspace")):
            raise WorktreeError("worker.uncertain", "worker receipt binding is incomplete")
        if receipt.get("repository_state") == "removed":
            try:
                os.lstat(directory / "workspace")
            except FileNotFoundError:
                pass
            else:
                raise WorktreeError("worker.uncertain", "retired repository unexpectedly exists")
        return receipt

    def inventory(self, session_id: str) -> dict:
        """Bound total directory entries and receipt bytes; unknown never means empty.

        No Git calls, liveness probes, locks, directory creation or repair occur.
        Unattributed entries make coverage unavailable for every session.
        """
        entries = []
        identities = set()
        view = {"root": str(self.root), "coverage": "complete", "entries": entries, "omitted": 0}
        try:
            root_fd = open_directory(self.root)
        except FileNotFoundError:
            # Only a missing root with a readable, trusted parent proves absence.
            parent = self.root.parent
            while True:
                try:
                    parent_fd = open_directory(parent)
                    os.close(parent_fd)
                    break
                except FileNotFoundError:
                    parent = parent.parent
                except (OSError, StorageError):
                    return {**view, "coverage": "unavailable", "omitted": None,
                            "reason": "worker storage cannot be inspected"}
            return view
        except (OSError, StorageError):
            return {**view, "coverage": "unavailable", "omitted": None,
                    "reason": "worker storage cannot be inspected"}
        try:
            with os.scandir(root_fd) as directories:
                for index, entry in enumerate(directories):
                    if index >= MAX_WORKERS:
                        view.update(coverage="bounded", omitted=None)
                        break
                    if entry.name == ".lock":
                        continue
                    try:
                        if not entry.name.startswith("worker-") or not entry.is_dir(follow_symlinks=False):
                            raise WorktreeError("worker.uncertain", "unrecognized worker entry")
                        receipt = self._read(self.root / entry.name)
                    except (OSError, StorageError, WorktreeError):
                        view.update(coverage="unavailable", omitted=None,
                                    reason="worker ownership cannot be attributed")
                        continue
                    if receipt["candidate_id"] in identities:
                        view.update(coverage="unavailable", omitted=None,
                                    reason="duplicate worker identity")
                    identities.add(receipt["candidate_id"])
                    if receipt["parent_session_id"] != session_id:
                        continue
                    entries.append({
                        "candidate_id": receipt["candidate_id"], "directory": entry.name,
                        "workspace": receipt["workspace"], "state": receipt["state"],
                        "cleanup": receipt.get("cleanup", "unknown"),
                        "repository_state": receipt.get("repository_state", "retained"),
                        "ownership": "recorded" if receipt.get("parent_ownership") else "unknown",
                    })
        except OSError:
            view.update(coverage="unavailable", omitted=None, reason="worker scan failed")
        finally:
            os.close(root_fd)
        return view

    def _settled(self, receipt: dict, *, parent_guard=None):
        from garuda.runtime.capacity import CapacityError, CapacityStore

        binding = receipt.get("parent_ownership")
        if not isinstance(binding, dict) or not isinstance(binding.get("lease"), dict):
            raise WorktreeError("worker.uncertain", "parent ownership was not recorded")
        lease = binding["lease"]
        parent = lease.get("workspace")
        workspace = Path(receipt["workspace"])
        if (not isinstance(parent, str) or not Path(parent).is_absolute()
                or lease.get("session_id") != receipt["parent_session_id"]
                or lease.get("mode") != "mutating"
                or not isinstance(lease.get("epoch"), str) or not lease["epoch"]
                or "\0" in parent
                or Path(parent).resolve() != Path(parent)
                or workspace.is_relative_to(Path(parent)) or Path(parent).is_relative_to(workspace)):
            raise WorktreeError("worker.uncertain", "parent workspace binding is invalid")
        leases = parent_guard.leases if parent_guard is not None else LeaseStore()
        lease_fd = open_directory(Path(os.path.abspath(leases.root)))
        os.close(lease_fd)
        if receipt.get("lease_root") != str(leases.root.resolve()):
            raise WorktreeError("worker.uncertain", "worker lease storage binding changed")
        if (not isinstance(binding.get("root"), str) or "\0" in binding["root"]
                or Path(binding["root"]).resolve() != leases.root.resolve()):
            raise WorktreeError("worker.uncertain", "lease storage binding changed")
        if parent_guard is not None:
            current = parent_guard.snapshot_lifetime()
            if current != binding:
                raise WorktreeError("worker.foreign_parent", "parent ownership changed")
        else:
            # Absence of the exact parent's lease is an issuing-owner release,
            # not a heartbeat expiry or a dead process being treated as settled.
            if not leases.released(parent):
                raise WorktreeError("worker.parent_owned", "parent workspace ownership is retained")
        if receipt.get("child_settled") is not True:
            raise WorktreeError("worker.uncertain", "child descendant settlement is unproved")
        if not leases.released(workspace):
            raise WorktreeError("worker.ownership_retained", "worker workspace ownership is retained")
        try:
            capacity = CapacityStore()
            capacity_fd = open_directory(Path(os.path.abspath(capacity.root)))
            os.close(capacity_fd)
            if (receipt.get("capacity_root") != str(capacity.root.resolve())
                    or not capacity.released("native", receipt["candidate_id"])):
                raise WorktreeError("worker.uncertain", "worker capacity release is unproved")
            if capacity.slots_for_holder(receipt["candidate_id"]):
                raise WorktreeError("worker.ownership_retained", "worker capacity is retained")
        except CapacityError as exc:
            raise WorktreeError("worker.uncertain", "worker capacity cannot be inspected") from exc
        return leases, capacity, [workspace] if parent_guard is not None else [workspace, parent]

    def _reflogs_unchanged(self, workspace: Path, base: str) -> None:
        # Git's reflog traversal omits epoch-zero entries (our deterministic
        # snapshots use that date), so inspect bounded raw logs without links.
        allowed = {base.encode(), b"0" * len(base)}
        seen = 0

        def visit(directory_fd):
            nonlocal seen
            with os.scandir(directory_fd) as entries:
                for entry in entries:
                    seen += 1
                    if seen > MAX_WORKERS:
                        raise WorktreeError("worker.uncertain", "worker reflogs exceed the inspection bound")
                    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
                    if entry.is_dir(follow_symlinks=False):
                        fd = os.open(entry.name, flags | os.O_DIRECTORY, dir_fd=directory_fd)
                        try:
                            visit(fd)
                        finally:
                            os.close(fd)
                    else:
                        fd = os.open(entry.name, flags, dir_fd=directory_fd)
                        with os.fdopen(fd, "rb") as handle:
                            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                                raise WorktreeError("worker.uncertain", "worker reflog is not a regular file")
                            raw = handle.read(65_537)
                        if len(raw) > 65_536 or any(
                            len(parts := line.split(b" ", 2)) != 3
                            or parts[0] not in allowed or parts[1] not in allowed
                            for line in raw.splitlines()
                        ):
                            raise WorktreeError("worker.unintegrated_changes", "worker retains additional reflog data")

        try:
            fd = open_directory(workspace / ".git" / "logs")
        except FileNotFoundError:
            return
        try:
            visit(fd)
        finally:
            os.close(fd)

    def _unchanged(self, receipt: dict) -> None:
        workspace = Path(receipt["workspace"])
        base = receipt.get("base_commit")
        if not isinstance(base, str) or not re.fullmatch(r"[0-9a-f]{40,64}", base):
            raise WorktreeError("worker.uncertain", "worker base was not recorded")
        tree = receipt.get("base_tree")
        if receipt["state"] == "integrated":
            provenance = receipt.get("provenance")
            tree = provenance.get("candidate_tree") if isinstance(provenance, dict) else None
        if not isinstance(tree, str) or not re.fullmatch(r"[0-9a-f]{40,64}", tree):
            raise WorktreeError("worker.uncertain", "worker tree was not recorded")
        # HEAD, index, extra refs, untracked and ignored data all matter. A clean
        # porcelain status alone would miss commits, ignored files and stash refs.
        if (snap.git(workspace, "rev-parse", "HEAD").stdout.decode().strip() != base
                or snap.git(workspace, "symbolic-ref", "-q", "HEAD", check=False).returncode != 1
                or snap.git(workspace, "for-each-ref", "--format=%(refname)").stdout
                != b"refs/garuda/snapshot\n"
                or snap.git(workspace, "rev-parse", "refs/garuda/snapshot").stdout.decode().strip() != base
                or snap.git(workspace, "diff", "--cached", "--quiet", "--no-ext-diff",
                            "--no-textconv", base, "--", check=False).returncode != 0
                or snap.git(workspace, "ls-files", "--others", "-z").stdout):
            raise WorktreeError("worker.unintegrated_changes", "worker has unintegrated or additional data")
        self._reflogs_unchanged(workspace, base)
        # Hash actual content through the existing bounded snapshot owner. Git
        # status/diff can hide edits behind assume-unchanged or skip-worktree.
        # Objects go only to scratch; inspection above never snapshots anything.
        with tempfile.TemporaryDirectory(prefix="garuda-worker-retirement-") as scratch:
            target = Path(scratch)
            snap.git(target, "init", "-q")
            current = snap.capture(workspace, object_repo=target)
        if current.tree != tree:
            raise WorktreeError("worker.unintegrated_changes", "worker content differs from its retained tree")

    def retire(self, session_id: str, directory_name: str, *, discard=False, parent_guard=None) -> dict:
        """Re-prove ownership before removal. Discard waives only the content check."""
        if Path(directory_name).name != directory_name or not directory_name.startswith("worker-"):
            raise WorktreeError("worker.uncertain", "worker directory is invalid")
        if self.inventory(session_id)["coverage"] != "complete":
            raise WorktreeError("worker.uncertain", "worker inventory is incomplete")
        directory = self.root / directory_name
        if self._read(directory)["parent_session_id"] != session_id:
            raise WorktreeError("worker.foreign_parent", "worker belongs to another session")
        validation_fd = open_directory(directory)
        try:
            with exclusive_lock(directory) as directory_fd:
                before, after = os.fstat(validation_fd), os.fstat(directory_fd)
                if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
                    raise WorktreeError("worker.uncertain", "worker directory changed")
                receipt = self._read(directory)
                if receipt["parent_session_id"] != session_id:
                    raise WorktreeError("worker.foreign_parent", "worker belongs to another session")
                if receipt.get("repository_state") == "removed":
                    return receipt
                leases, capacity, released = self._settled(receipt, parent_guard=parent_guard)
                if not discard and receipt["state"] not in _TERMINAL:
                    raise WorktreeError("worker.candidate_retained", "worker candidate is still available")
                with leases.guard_released(released), capacity.guard_released("native", receipt["candidate_id"]):
                    return self._remove_repository(directory_fd, directory, receipt, discard=discard)
        finally:
            os.close(validation_fd)

    def _remove_repository(self, directory_fd, directory, receipt, *, discard):
        workspace = directory / "workspace"
        removing = receipt.get("repository_state") == "removing"
        try:
            identity = repository_identity(workspace)
        except FileNotFoundError:
            if not removing:
                raise WorktreeError("worker.uncertain", "repository is missing without retirement intent") from None
            receipt.update(repository_state="removed")
            write_locked_document(directory_fd, directory / "receipt.json", receipt)
            return receipt
        if identity != receipt.get("repository_identity"):
            raise WorktreeError("worker.uncertain", "repository identity changed or is unknown")
        if not (discard and removing):
            git_fd = open_directory(workspace / ".git")
            os.close(git_fd)
            if (snap.git(workspace, "rev-parse", "--absolute-git-dir").stdout.decode().strip()
                    != str(workspace / ".git")
                    or snap.git(workspace, "rev-parse", "--show-toplevel").stdout.decode().strip()
                    != str(workspace)):
                raise WorktreeError("worker.uncertain", "repository is linked or redirected")
        if not discard:
            self._unchanged(receipt)
        if not shutil.rmtree.avoids_symlink_attacks:
            raise WorktreeError("worker.uncertain", "safe directory removal is unavailable")
        receipt.update(repository_state="removing", discarded=bool(discard))
        write_locked_document(directory_fd, directory / "receipt.json", receipt)
        if repository_identity(workspace) != receipt["repository_identity"]:
            raise WorktreeError("worker.uncertain", "repository changed before removal")
        shutil.rmtree("workspace", dir_fd=directory_fd)
        receipt.update(repository_state="removed")
        write_locked_document(directory_fd, directory / "receipt.json", receipt)
        return receipt

    def recover(self, session_id: str, *, parent_guard=None) -> dict:
        """Best-effort automatic cleanup; every uncertainty stays retained."""
        view = self.inventory(session_id)
        if view["coverage"] != "complete":
            return view
        for entry in view["entries"]:
            if entry["repository_state"] == "removed":
                continue
            try:
                self.retire(session_id, entry["directory"], parent_guard=parent_guard)
            except (OSError, StorageError, LeaseError, CapacityError, WorktreeError, snap.SnapshotRefused):
                pass
        return self.inventory(session_id)

    def discard(self, session_id: str, candidate_id: str) -> dict:
        view = self.inventory(session_id)
        if view["coverage"] != "complete":
            raise WorktreeError("worker.uncertain", "worker inventory is incomplete")
        matches = [e for e in view["entries"] if e["candidate_id"] == candidate_id]
        if len(matches) != 1:
            raise WorktreeError("worker.uncertain", "worker identity is missing or ambiguous")
        return self.retire(session_id, matches[0]["directory"], discard=True)
