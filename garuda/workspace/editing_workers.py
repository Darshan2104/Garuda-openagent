"""Bounded local editing-worker inventory and conservative repository retirement (#384).

Retained receipts remain as audit records; later cleanup compacts fully retired
ones. Retiring a repository never releases leases or capacity: missing
descendant-settlement evidence requires supervised recovery.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import tempfile
import time
import uuid
from itertools import islice
from pathlib import Path

from garuda.runtime.capacity import CapacityError
from garuda.runtime.private_records import open_directory, open_private_directory, read_record
from garuda.runtime.strict_store import StorageError, exclusive_lock, write_locked_document
from garuda.workspace import snapshot_proto as snap
from garuda.workspace.lease import LeaseError, LeaseStore
from garuda.workspace.worktrees import WorktreeError, worktrees_root

MAX_WORKERS = 128
STAGING_PREFIX = ".admit-"
_WORKSPACE = "workspace"
_RETIRING = "workspace.retiring"
_ADMISSION_FILE = re.compile(r"(?:\.lock|receipt\.json|\.receipt\.json\.[0-9]+\.[0-9a-f]{32}\.tmp)")
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
        fd = open_private_directory(directory)
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
            for name in (_WORKSPACE, _RETIRING):
                try:
                    os.lstat(directory / name)
                except FileNotFoundError:
                    continue
                raise WorktreeError("worker.uncertain", "retired repository unexpectedly exists")
        return receipt

    def inventory(self, session_id: str) -> dict:
        """Bound this session's entries and receipt bytes; unknown never means empty.

        No Git calls, liveness probes, locks, directory creation or repair occur.
        Valid receipts of other sessions do not consume this session's bound.
        An unattributable non-empty entry or an unreadable private directory
        still makes coverage unavailable.
        Receipt-less empty/staging directories are reported as removable orphans.
        """
        entries = []
        orphans = []
        identities = set()
        view = {"root": str(self.root), "coverage": "complete", "entries": entries,
                "orphans": orphans, "omitted": 0}
        try:
            root_fd = open_private_directory(self.root)
        except FileNotFoundError:
            parent = self.root.parent
            while True:
                try:
                    parent_fd = open_directory(parent, private_ancestors=False)
                    os.close(parent_fd)
                    return view
                except FileNotFoundError:
                    parent = parent.parent
                except (OSError, StorageError) as exc:
                    return {**view, "coverage": "unavailable", "omitted": None,
                            "reason": f"worker storage cannot be inspected: {exc}"}
        except (OSError, StorageError) as exc:
            return {**view, "coverage": "unavailable", "omitted": None,
                    "reason": f"worker storage cannot be inspected: {exc}"}
        try:
            with os.scandir(root_fd) as directories:
                for entry in directories:
                    if entry.name == ".lock":
                        continue
                    try:
                        if not entry.is_dir(follow_symlinks=False):
                            raise WorktreeError("worker.uncertain", "unrecognized worker entry")
                        if not entry.name.startswith("worker-"):
                            try:
                                state = self._orphan_state(self.root / entry.name, entry.name)
                            except FileNotFoundError:
                                continue
                            if state is None:
                                raise WorktreeError("worker.uncertain", "unrecognized worker entry")
                            orphans.append({"directory": entry.name, "state": state})
                            continue
                        try:
                            receipt = self._read(self.root / entry.name)
                        except FileNotFoundError:
                            # A concurrent cleanup pruned this entry while scanning.
                            continue
                        except (OSError, StorageError, WorktreeError):
                            state = self._orphan_state(self.root / entry.name, entry.name)
                            if state is None:
                                raise
                            orphans.append({"directory": entry.name, "state": state})
                            continue
                    except (OSError, StorageError, WorktreeError) as exc:
                        view.update(coverage="unavailable", omitted=None,
                                    reason=f"worker ownership cannot be attributed: {exc}")
                        break
                    if receipt["parent_session_id"] != session_id:
                        continue
                    if receipt["candidate_id"] in identities:
                        view.update(coverage="unavailable", omitted=None,
                                    reason="duplicate worker identity")
                        break
                    if len(entries) >= MAX_WORKERS:
                        view.update(coverage="bounded", omitted=None)
                        break
                    identities.add(receipt["candidate_id"])
                    entries.append({
                        "candidate_id": receipt["candidate_id"], "directory": entry.name,
                        "workspace": receipt["workspace"], "state": receipt["state"],
                        "cleanup": receipt.get("cleanup", "unknown"),
                        "repository_state": receipt.get("repository_state", "retained"),
                        "ownership": "recorded" if receipt.get("parent_ownership") else "unknown",
                    })
        except OSError as exc:
            view.update(coverage="unavailable", omitted=None,
                        reason=f"worker scan failed: {exc}")
        finally:
            os.close(root_fd)
        return view

    def _orphan_state(self, directory: Path, name: str) -> str | None:
        """Recognize empty leftovers or unpublished admission metadata only."""
        if not name.startswith(("worker-", STAGING_PREFIX)):
            return None
        try:
            fd = open_directory(directory, private_ancestors=False)
        except FileNotFoundError:
            raise
        except (OSError, StorageError):
            return None
        try:
            try:
                if os.fstat(fd).st_uid != os.getuid():
                    return None
                with os.scandir(fd) as contents:
                    files = list(islice(contents, 4))
                if not files:
                    return "staging" if name.startswith(STAGING_PREFIX) else "empty"
                if (not name.startswith(STAGING_PREFIX) or len(files) > 3
                        or stat.S_IMODE(os.fstat(fd).st_mode) != 0o700
                        or any(not _ADMISSION_FILE.fullmatch(file.name)
                               or not file.is_file(follow_symlinks=False)
                               or file.stat(follow_symlinks=False).st_uid != os.getuid()
                               or stat.S_IMODE(file.stat(follow_symlinks=False).st_mode) != 0o600
                               for file in files)):
                    return None
                candidate = name.removeprefix(STAGING_PREFIX)[:36]
                if str(uuid.UUID(candidate)) != candidate or not name.startswith(f"{STAGING_PREFIX}{candidate}-"):
                    return None
                receipt = read_record(fd, "receipt.json")
                if receipt is None:
                    return "staging"
                if receipt.get("state") != "admitted":
                    return None
                if (receipt.get("candidate_id") != candidate
                        or receipt.get("workspace") != str(self.root / f"worker-{candidate}" / _WORKSPACE)):
                    return None
                return "staging"
            finally:
                os.close(fd)
        except (OSError, StorageError, ValueError):
            return None

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
        lease_fd = open_private_directory(Path(os.path.abspath(leases.root)))
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
            capacity_fd = open_private_directory(Path(os.path.abspath(capacity.root)))
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
            fd = open_directory(workspace / ".git" / "logs", private_ancestors=False)
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
        """Re-prove ownership before removal. Discard waives only the content check.

        Git, hashing and settlement reads run before the global lease/capacity
        locks. Those locks cover only the cheap recheck, the intent receipt and
        the atomic rename that unpublishes the repository. Deletion runs after
        they are dropped, so another session is never blocked on recursion.
        """
        if Path(directory_name).name != directory_name or not directory_name.startswith("worker-"):
            raise WorktreeError("worker.uncertain", "worker directory is invalid")
        if self.inventory(session_id)["coverage"] != "complete":
            raise WorktreeError("worker.uncertain", "worker inventory is incomplete")
        directory = self.root / directory_name
        initial = self._read(directory)
        if initial["parent_session_id"] != session_id:
            raise WorktreeError("worker.foreign_parent", "worker belongs to another session")
        validation_fd = open_private_directory(directory)
        try:
            with exclusive_lock(directory) as directory_fd:
                before, after = os.fstat(validation_fd), os.fstat(directory_fd)
                if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
                    raise WorktreeError("worker.uncertain", "worker directory changed")
                receipt = self._read(directory)
                if receipt != initial:
                    raise WorktreeError("worker.uncertain", "worker receipt changed during retirement")
                if receipt.get("repository_state") == "removed":
                    return receipt
                leases, capacity, released = self._settled(receipt, parent_guard=parent_guard)
                if not discard and receipt["state"] not in _TERMINAL:
                    raise WorktreeError("worker.candidate_retained", "worker candidate is still available")
                source = self._prove_repository(directory, receipt, discard=discard)
                with leases.guard_released(released), capacity.guard_released(
                    "native", receipt["candidate_id"],
                ):
                    if parent_guard is not None:
                        binding = receipt["parent_ownership"]
                        issued = leases.snapshot_issued(
                            parent_guard.workspace, parent_guard.session_id,
                            epoch=binding["lease"]["epoch"],
                        )
                        holders = [holder.to_dict() for holder in leases.holders_of(parent_guard.workspace)]
                        if (issued != binding or not any(
                            all(holder.get(key) == value for key, value in binding["lease"].items())
                            for holder in holders
                        )):
                            raise WorktreeError("worker.foreign_parent", "parent ownership changed")
                    if self._read(directory) != receipt:
                        raise WorktreeError("worker.uncertain", "worker receipt changed during proof")
                    for name in (_WORKSPACE, _RETIRING):
                        if name != source and os.path.lexists(directory / name):
                            raise WorktreeError("worker.uncertain", "repository appeared during proof")
                    if source is not None:
                        if repository_identity(directory / source) != receipt.get("repository_identity"):
                            raise WorktreeError("worker.uncertain", "repository changed before removal")
                    receipt.update(repository_state="removing", discarded=bool(discard))
                    write_locked_document(directory_fd, directory / "receipt.json", receipt)
                    if source == _WORKSPACE:
                        os.rename(_WORKSPACE, _RETIRING, src_dir_fd=directory_fd,
                                  dst_dir_fd=directory_fd)
                        os.fsync(directory_fd)
                # Keep only the per-worker lock through deletion and completion.
                # Other sessions can acquire lease and capacity locks throughout.
                if source is not None:
                    if repository_identity(directory / _RETIRING) != receipt.get("repository_identity"):
                        raise WorktreeError("worker.uncertain", "retired repository staging changed")
                    shutil.rmtree(_RETIRING, dir_fd=directory_fd)
                receipt.update(repository_state="removed", retired_ns=time.time_ns())
                write_locked_document(directory_fd, directory / "receipt.json", receipt)
                return receipt
        finally:
            os.close(validation_fd)

    def _prove_repository(self, directory: Path, receipt: dict, *, discard: bool) -> str | None:
        """Check identity, links and content without any global lock."""
        if not shutil.rmtree.avoids_symlink_attacks:
            raise WorktreeError("worker.uncertain", "safe directory removal is unavailable")
        workspace = directory / _WORKSPACE
        removing = receipt.get("repository_state") == "removing"
        try:
            staged_identity = repository_identity(directory / _RETIRING)
        except FileNotFoundError:
            staged_identity = None
        if staged_identity is not None:
            if (not removing or staged_identity != receipt.get("repository_identity")
                    or os.path.lexists(workspace)):
                raise WorktreeError("worker.uncertain", "retired repository staging changed")
            # The receipt's intent precedes the rename, after the content proof.
            return _RETIRING
        try:
            identity = repository_identity(workspace)
        except FileNotFoundError:
            if not removing:
                raise WorktreeError(
                    "worker.uncertain", "repository is missing without retirement intent",
                ) from None
            return None
        if identity != receipt.get("repository_identity"):
            raise WorktreeError("worker.uncertain", "repository identity changed or is unknown")
        if not (discard and removing):
            git_fd = open_directory(workspace / ".git", private_ancestors=False)
            os.close(git_fd)
            if (snap.git(workspace, "rev-parse", "--absolute-git-dir").stdout.decode().strip()
                    != str(workspace / ".git")
                    or snap.git(workspace, "rev-parse", "--show-toplevel").stdout.decode().strip()
                    != str(workspace)):
                raise WorktreeError("worker.uncertain", "repository is linked or redirected")
        if not discard:
            self._unchanged(receipt)
        return _WORKSPACE

    def _prune_orphans(self) -> None:
        """Remove only empty directories or recognized admission metadata."""
        try:
            with exclusive_lock(self.root) as root_fd:
                with os.scandir(root_fd) as directories:
                    for entry in directories:
                        try:
                            if self._orphan_state(self.root / entry.name, entry.name) is None:
                                continue
                            if entry.name.startswith(STAGING_PREFIX):
                                fd = open_directory(self.root / entry.name, private_ancestors=False)
                                try:
                                    with os.scandir(fd) as contents:
                                        files = list(islice(contents, 4))
                                    if len(files) > 3 or any(not _ADMISSION_FILE.fullmatch(file.name) for file in files):
                                        continue
                                    for file in files:
                                        try:
                                            os.unlink(file.name, dir_fd=fd)
                                        except FileNotFoundError:
                                            pass
                                finally:
                                    os.close(fd)
                            os.rmdir(entry.name, dir_fd=root_fd)
                        except OSError:
                            continue
        except (OSError, StorageError):
            return

    def _prune_retired(self, run_ns: int, *, keep_candidate: str | None = None) -> None:
        """Drop only known audit files from a previously retired directory."""
        try:
            with exclusive_lock(self.root) as root_fd:
                with os.scandir(root_fd) as directories:
                    for entry in directories:
                        if (not entry.name.startswith("worker-")
                                or not entry.is_dir(follow_symlinks=False)):
                            continue
                        directory = self.root / entry.name
                        try:
                            receipt = self._read(directory)
                            if (receipt.get("repository_state") != "removed"
                                    or receipt.get("candidate_id") == keep_candidate):
                                continue
                            retired = receipt.get("retired_ns")
                            if retired is not None and (type(retired) is not int or retired >= run_ns):
                                continue
                            with exclusive_lock(directory) as directory_fd:
                                if self._read(directory) != receipt:
                                    continue
                                with os.scandir(directory_fd) as contents:
                                    files = list(islice(contents, 4))
                                if any(file.name not in {".lock", "receipt.json", "candidate.patch"}
                                       or not file.is_file(follow_symlinks=False)
                                       or file.stat(follow_symlinks=False).st_uid != os.getuid()
                                       for file in files):
                                    continue
                                for file in files:
                                    if file.name != "receipt.json":
                                        os.unlink(file.name, dir_fd=directory_fd)
                                os.unlink("receipt.json", dir_fd=directory_fd)
                                os.rmdir(entry.name, dir_fd=root_fd)
                        except (OSError, StorageError, WorktreeError):
                            continue
        except (OSError, StorageError):
            return

    def recover(self, session_id: str, *, parent_guard=None) -> dict:
        """Best-effort automatic cleanup; every uncertainty stays retained."""
        run_ns = time.time_ns()
        self._prune_orphans()
        self._prune_retired(run_ns)
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
        self._prune_orphans()
        self._prune_retired(time.time_ns(), keep_candidate=candidate_id)
        view = self.inventory(session_id)
        if view["coverage"] != "complete":
            raise WorktreeError("worker.uncertain", "worker inventory is incomplete")
        matches = [e for e in view["entries"] if e["candidate_id"] == candidate_id]
        if len(matches) != 1:
            raise WorktreeError("worker.uncertain", "worker identity is missing or ambiguous")
        return self.retire(session_id, matches[0]["directory"], discard=True)
