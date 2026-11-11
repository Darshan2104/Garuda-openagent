"""Sequential native editing workers with detached repositories and checked integration."""

from __future__ import annotations

import asyncio
import hashlib
import tempfile
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Protocol

from garuda.core.child_result import build_child_result_envelope, canonical_json
from garuda.core.events import EventType
from garuda.core.permissions import (
    PermissionDecision,
    _resolve_decision,
    permission_workspace,
)
from garuda.interfaces.run_guard import WorkspaceLeaseGuard
from garuda.runtime.capacity import CapacityError, CapacityStore, configured_ceiling
from garuda.runtime.strict_store import StorageError, exclusive_lock, write_locked_document
from garuda.types import AgentResult
from garuda.workspace import snapshot_proto as snap
from garuda.workspace.editing_workers import (
    EditingWorkerRepositories,
    editing_workers_root,
    repository_identity,
)
from garuda.workspace.health import HealthMonitoredEnvironment
from garuda.workspace.lease import LeaseError
from garuda.workspace.local import LocalEnvironment
from garuda.workspace.settlement import settle_task
from garuda.workspace.worktrees import (
    WorktreeError,
    apply_snapshot_patch,
    check_snapshot_tree,
    snapshot_patch_paths,
)


class EditingWorkerService(Protocol):
    async def run(self, runner, profile_name, task, *, fork_parent_context=None) -> AgentResult: ...

    async def integrate(self, candidate_id, provenance_digest, *, env, session_id, permissions) -> dict: ...


def _digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


class _RelocatedPermissions:
    """Screen both actual paths and their corresponding parent workspace paths."""

    def __init__(self, parent, source: str, worker: str):
        self.parent, self.source, self.worker = parent, source, worker

    def __getattr__(self, name):
        return getattr(self.parent, name)

    def _map(self, value):
        if isinstance(value, str):
            return value.replace(self.worker, self.source)
        if isinstance(value, dict):
            return {key: self._map(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self._map(item) for item in value]
        return value

    def decide(self, name, arguments):
        return self._decide(self.parent.decide, name, arguments)

    def _decide(self, method, *arguments):
        actual = method(*arguments)
        with permission_workspace(self.source):
            mapped = method(*(self._map(item) for item in arguments))
        for decision in (PermissionDecision.DENY, PermissionDecision.ASK):
            for outcome in (actual, mapped):
                if outcome[0] == decision:
                    return outcome
        return actual

    def decide_acceptance(self, arguments):
        return self._decide(self.parent.decide_acceptance, arguments)

    async def evaluate_acceptance(self, arguments):
        decision, reason = self.decide_acceptance(arguments)
        return await _resolve_decision(decision, reason, "bash", arguments, self.approval_handler)

    def check_path(self, path, operation, *, cwd=None):
        actual = self.parent.check_path(path, operation, cwd=cwd)
        with permission_workspace(self.source):
            mapped = self.parent.check_path(self._map(path), operation, cwd=self._map(cwd))
        return max((actual, mapped), key=lambda d: {
            PermissionDecision.ALLOW: 0, PermissionDecision.ASK: 1, PermissionDecision.DENY: 2,
        }[d])


@dataclass
class _Worker:
    directory: Path
    workspace: Path
    base: snap.Snapshot | None
    index_digest: str
    head_ref: bytes
    guard: WorkspaceLeaseGuard
    receipt: dict
    events: object
    patch: bytes = b""
    tree: str = ""


class LocalEditingWorkers:
    """Trusted programmatic opt-in, bound to one held mutating parent lease.

    Receipts stay retained. Settled repositories retire only after ownership and
    content checks; uncertain descendants retain their lease and capacity. No
    parallel dispatch or implicit integration authority is supplied here.
    """

    def __init__(self, parent_guard: WorkspaceLeaseGuard, *, checks: list[str],
                 image: str, root: str | Path | None = None, timeout: float = 600):
        self.parent_guard = parent_guard
        self.checks = tuple(checks)
        self.image, self.timeout = image, timeout
        self.root = Path(root) if root is not None else editing_workers_root()
        self.repositories = EditingWorkerRepositories(self.root)
        self._workers: dict[str, _Worker] = {}
        self._busy = False

    def _authorize(self, env, session_id):
        backend = env
        if type(env) is HealthMonitoredEnvironment:
            if not env.is_alive:
                raise WorktreeError("worker.environment_unavailable", "parent environment is unavailable")
            backend = env.inner
        if type(backend) is not LocalEnvironment:
            raise WorktreeError("worker.unsupported_environment", "editing workers currently require local")
        if (session_id != self.parent_guard.session_id
                or Path(env.workspace_root).resolve() != Path(self.parent_guard.workspace).resolve()):
            raise WorktreeError("worker.foreign_parent", "the service belongs to another parent")
        self.parent_guard.snapshot_ownership()
        self.parent_guard.snapshot_lifetime()
        inventory = self.repositories.recover(session_id, parent_guard=self.parent_guard)
        if inventory["coverage"] != "complete":
            raise WorktreeError("worker.uncertain", "retained worker ownership cannot be read completely")
        if any(entry["repository_state"] != "removed" and entry["state"] not in {
            "ready", "failed", "integrated", "refused",
        } for entry in inventory["entries"]):
            raise WorktreeError("worker.uncertain", "retained worker cleanup is unproved")
        if not self.checks or not self.image:
            raise WorktreeError("integration.no_trusted_check", "configure trusted integration checks")

    def _record(self, worker, state, **fields):
        worker.receipt.update(state=state, **fields)
        with exclusive_lock(worker.directory) as directory_fd:
            write_locked_document(directory_fd, worker.directory / "receipt.json",
                                  {"version": 1, **worker.receipt})
        worker.events.append(EventType.EDITING_WORKER, dict(worker.receipt))

    def _retire(self, worker):
        try:
            receipt = self.repositories.retire(
                worker.receipt["parent_session_id"], worker.directory.name,
                parent_guard=self.parent_guard,
            )
        except (OSError, StorageError, LeaseError, CapacityError, WorktreeError, snap.SnapshotRefused):
            return
        worker.receipt.update(receipt)

    async def _quarantine(self, worker):
        # Mark ownership before any cancellable cleanup or evidence publication.
        await worker.guard.stop_heartbeat()
        self._record(worker, "uncertain", cleanup="unknown")

    async def run(self, runner, profile_name, task, *, fork_parent_context=None):
        from garuda.agents.setup import resolve_profile
        from garuda.core.subagent import _refused
        from garuda.tools import (
            BashTool,
            BufferGrepTool,
            BufferListTool,
            BufferQueryTool,
            BufferSliceTool,
            ContractTool,
            EditTool,
            GlobTool,
            GrepTool,
            ImageReadTool,
            LsTool,
            MultiEditTool,
            ReadFileTool,
            ReadPdfTool,
            ReadSpreadsheetTool,
            TaskCompleteTool,
            TodoTool,
            UpdateGoalTool,
            WebFetchTool,
            WebSearchTool,
            WriteFileTool,
        )

        worker = None
        owns_busy = False
        try:
            self._authorize(runner.env, runner.events.session_id)
            if self._busy:
                raise WorktreeError("worker.busy", "an editing operation is already running")
            if runner.parent_permissions is None or runner.parent_tools is None:
                raise WorktreeError("worker.no_parent_authority", "parent tools and permissions are required")
            profile = resolve_profile(profile_name, runner.workspace_root or ".", runner.agents_dir)
            if profile.to_agent_config().output_schema is None:
                raise WorktreeError("worker.typed_output_required", "editing profiles must declare an output schema")
            self._busy = True
            owns_busy = True
            source = Path(runner.env.workspace_root).resolve()
            root = self.root.resolve()
            if root.is_relative_to(source):
                raise WorktreeError("worker.invalid_root", "worker storage must be outside the parent workspace")
            root.mkdir(mode=0o700, parents=True, exist_ok=True)
            directory = Path(tempfile.mkdtemp(prefix="worker-", dir=root))
            workspace = directory / "workspace"
            worker_id = str(uuid.uuid4())
            guard = WorkspaceLeaseGuard(str(workspace), worker_id, capacity_key="native",
                                        capacity_ceiling=configured_ceiling("native") or 1)
            # A retained admission record precedes snapshot creation and child execution.
            index = _digest(snap.git(source, "ls-files", "-s", "-z").stdout)
            head_ref = snap.git(source, "symbolic-ref", "-q", "HEAD", check=False).stdout
            worker = _Worker(directory, workspace, None, index, head_ref, guard, {
                "candidate_id": worker_id, "parent_session_id": runner.events.session_id,
                "workspace": str(workspace), "cleanup": "unknown",
                "parent_ownership": self.parent_guard.snapshot_lifetime(),
                "lease_root": str(guard.leases.root.resolve()),
                "capacity_root": str(CapacityStore().root.resolve()),
            }, runner.events)
            self._workers[worker_id] = worker
            self._record(worker, "admitted")
            guard.acquire()
            guard.start_heartbeat()
            worker.base = snap.detached_repository(source, workspace)
            snap.git(workspace, "update-ref", "--no-deref", "HEAD", worker.base.commit)
            child_env = LocalEnvironment(workspace)
            # Only stateless built-in tools execute in this new Environment. Bound
            # MCP, tmux, consult, memory, collection and nested delegation stay out.
            native_types = {
                BashTool, BufferGrepTool, BufferListTool, BufferQueryTool, BufferSliceTool,
                ContractTool, EditTool, GlobTool, GrepTool, ImageReadTool, LsTool,
                MultiEditTool, ReadFileTool, ReadPdfTool, ReadSpreadsheetTool, TaskCompleteTool,
                TodoTool, UpdateGoalTool, WebFetchTool, WebSearchTool, WriteFileTool,
            }
            tools = [tool for tool in runner.parent_tools if type(tool) in native_types]
            child = replace(
                runner, env=child_env, workspace_root=str(workspace),
                workspace_kind="local", workspace_evidence_root=str(workspace),
                child_session_id=worker_id, editing_workers=None, busy=False,
                parent_tools=tools, notes=None, parent_buffer=None, parent_context=None,
                parent_messages=None, allowed=[],
                parent_permissions=_RelocatedPermissions(runner.parent_permissions, str(source), str(workspace)),
            )
            self._record(worker, "running", repository_identity=repository_identity(workspace),
                         base_commit=worker.base.commit,
                         source_head=worker.base.source_head, base_tree=worker.base.tree,
                         base_manifest_digest=worker.base.manifest_digest)
            result = await guard.race(child._run_child(profile_name, task, fork_parent_context="none"))
            await child_env.aclose()
            effects = result.metadata.get("side_effects")
            if (not isinstance(effects, dict)
                    or not isinstance(effects.get("processes_started"), list)
                    or effects["processes_started"]):
                await self._quarantine(worker)
                result.success = False
            envelope = build_child_result_envelope(
                result, profile_name, storage=None, parent_tools=None, output_budget=None,
            )
            if worker.receipt["state"] != "uncertain":
                if not result.success or envelope["outcome"]["validation"] != "accepted":
                    self._record(worker, "failed", cleanup="settled")
                else:
                    candidate = snap.capture(workspace)
                    worker.tree = candidate.tree
                    worker.patch = snap.git(
                        workspace, "diff", "--binary", "--full-index", "--no-ext-diff", "--no-textconv",
                        worker.base.commit, candidate.commit, "--",
                    ).stdout
                    patch_path = directory / "candidate.patch"
                    with patch_path.open("xb") as handle:
                        handle.write(worker.patch)
                    provenance = {
                        "candidate_id": worker_id, "parent_session_id": runner.events.session_id,
                        "base_commit": worker.base.commit, "source_head": worker.base.source_head,
                        "base_tree": worker.base.tree, "base_manifest_digest": worker.base.manifest_digest,
                        "index_digest": index, "head_ref": head_ref.decode(),
                        "candidate_tree": worker.tree, "patch_digest": _digest(worker.patch),
                        "producer": envelope["producer"],
                        "output_contract_digest": _digest(canonical_json(result.metadata["output_contract"]).encode()),
                    }
                    self._record(worker, "ready", cleanup="settled", patch=str(patch_path),
                                 provenance_digest=_digest(canonical_json(provenance).encode()),
                                 provenance=provenance)
                await guard.release()
                self._record(worker, worker.receipt["state"], child_settled=True)
                self._retire(worker)
            result.metadata["editing_worker"] = dict(worker.receipt)
            return result
        except asyncio.CancelledError as cancelled:
            if worker is not None:
                try:
                    await settle_task(asyncio.create_task(self._quarantine(worker)))
                except Exception as exc:
                    raise cancelled from exc
            raise
        except (WorktreeError, snap.SnapshotRefused, CapacityError, LeaseError) as exc:
            if worker is not None:
                await self._quarantine(worker)
            return _refused(getattr(exc, "code", "worker.admission_failed"), str(exc))
        except BaseException:
            if worker is not None:
                await self._quarantine(worker)
            raise
        finally:
            if owns_busy:
                self._busy = False

    async def integrate(self, candidate_id, provenance_digest, *, env, session_id, permissions):
        worker = self._workers.get(candidate_id)
        owns_busy = False
        try:
            self._authorize(env, session_id)
            if self._busy:
                raise WorktreeError("worker.busy", "an editing operation is already running")
            if (worker is None or worker.receipt["state"] != "ready"
                    or provenance_digest != worker.receipt.get("provenance_digest")):
                raise WorktreeError("integration.invalid_candidate", "candidate is foreign, unavailable or changed")
            self._busy = True
            owns_busy = True
            patch = (worker.directory / "candidate.patch").read_bytes()
            if (patch != worker.patch or snap.capture(worker.workspace).tree != worker.tree):
                raise WorktreeError("integration.candidate_changed", "candidate patch or workspace changed")
            if permissions is None:
                raise WorktreeError("integration.permission_denied", "parent permissions are required")
            # Integration must pass the parent's actual write policy for every path,
            # including deleted paths. A tool-level allow alone cannot authorize it.
            parent = Path(env.workspace_root)
            current = snap.capture(parent)
            if current.tree != worker.base.tree:
                raise WorktreeError("integration.stale_base", "parent workspace changed since admission")
            paths = snapshot_patch_paths(parent, patch, current.tree, worker.tree)
            for path in paths:
                allowed, _ = await permissions.evaluate_tool_call("write_file", {"path": path})
                if not allowed:
                    raise WorktreeError("integration.permission_denied", f"parent refused {path}")
            for command in self.checks:
                allowed, _ = await permissions.evaluate_acceptance({"command": command})
                if not allowed:
                    raise WorktreeError("integration.permission_denied", "parent refused an integration check")
            check_task = asyncio.create_task(asyncio.to_thread(
                check_snapshot_tree, parent, worker.tree, checks=list(self.checks),
                image=self.image, timeout=self.timeout,
            ))
            try:
                checks = await asyncio.shield(check_task)
            except asyncio.CancelledError as cancelled:
                try:
                    await settle_task(check_task)
                except Exception as exc:
                    raise cancelled from exc
                raise
            self._authorize(env, session_id)
            if ((worker.directory / "candidate.patch").read_bytes() != patch
                    or snap.capture(worker.workspace).tree != worker.tree):
                raise WorktreeError("integration.candidate_changed", "candidate changed during checks")
            # No await between destination revalidation and apply. A crash after
            # this intent never produces a replayable candidate on restart.
            self._record(worker, "integrating", checks=checks)
            apply_snapshot_patch(Path(env.workspace_root), patch, worker.base,
                                 index_digest=worker.index_digest.removeprefix("sha256:"),
                                 head_ref=worker.head_ref, candidate_tree=worker.tree)
            self._record(worker, "integrated")
            self._retire(worker)
            return dict(worker.receipt)
        except asyncio.CancelledError as cancelled:
            if owns_busy and worker is not None:
                try:
                    self._record(worker, "uncertain", cleanup="unknown")
                except Exception as exc:
                    raise cancelled from exc
            raise
        except (WorktreeError, snap.SnapshotRefused, LeaseError, OSError, StorageError) as exc:
            uncertain = (owns_busy and worker is not None
                         and worker.receipt["state"] in {"integrating", "integrated"}
                         and not isinstance(exc, WorktreeError))
            outcome = {"state": "uncertain" if uncertain else "refused", "candidate_id": candidate_id,
                       "code": getattr(exc, "code", "integration.failed"), "message": str(exc)}
            if owns_busy and worker is not None:
                self._record(worker, outcome["state"], integration=outcome)
            return outcome
        finally:
            if owns_busy:
                self._busy = False
