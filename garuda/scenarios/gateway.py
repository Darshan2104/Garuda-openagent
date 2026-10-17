"""Loop-owned, at-most-once admission for dashboard flow and role-run starters."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import stat
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from garuda.core import setup_view
from garuda.core.sessions import SessionStore
from garuda.runtime.ownership import current_owner, owner_liveness
from garuda.runtime.strict_store import StorageError
from garuda.scenarios.authority import start_authority
from garuda.scenarios.catalog import VERSION as CATALOG_VERSION
from garuda.scenarios.compile import COMPILER_VERSION, compile_with_context
from garuda.scenarios.digests import digest
from garuda.scenarios.launch_intents import (
    IntentConflict,
    IntentStateError,
    LaunchIntentStore,
)
from garuda.scenarios.service import RESOLUTION_ERRORS, readiness

_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
logger = logging.getLogger(__name__)


class FlowStartError(Exception):
    def __init__(self, code: str, message: str, *, status: int = 409):
        super().__init__(message)
        self.code, self.status = code, status


@dataclass(frozen=True)
class FlowStartRequest:
    operation_id: str
    starter_id: str
    inputs: dict
    workspace: int
    preview_digest: str
    authority_digest: str

    @classmethod
    def parse(cls, value: Any) -> "FlowStartRequest":
        fields = {"operation_id", "starter_id", "inputs", "workspace",
                  "preview_digest", "authority_digest"}
        if type(value) is not dict or set(value) != fields:
            raise FlowStartError(
                "starter.start_invalid",
                "Start needs exactly operation_id, starter_id, inputs, workspace, "
                "preview_digest and authority_digest.", status=400)
        operation = value.get("operation_id")
        try:
            parsed = uuid.UUID(operation)
        except (ValueError, TypeError, AttributeError):
            parsed = None
        if parsed is None or parsed.version != 4 or str(parsed) != operation:
            raise FlowStartError("starter.operation_invalid",
                                 "operation_id must be a canonical UUIDv4.", status=400)
        if (not isinstance(value.get("starter_id"), str)
                or not isinstance(value.get("inputs"), dict)
                or type(value.get("workspace")) is not int
                or any(not isinstance(value.get(name), str)
                       or _DIGEST.fullmatch(value[name]) is None
                       for name in ("preview_digest", "authority_digest"))):
            raise FlowStartError("starter.start_invalid",
                                 "Start fields have invalid types or digests.", status=400)
        return cls(operation, value["starter_id"], value["inputs"], value["workspace"],
                   value["preview_digest"], value["authority_digest"])


_REASON = re.compile(r"[a-z][a-z0-9_.-]{0,79}\Z")


def _failure_reason(exc: BaseException) -> tuple[str, bool]:
    """A bounded intent reason, and whether the error is a pre-launch refusal.

    Only refusals that the runner raises before reserving its parent qualify;
    the caller must still prove the reserved parent never appeared.
    """
    from garuda.flows.engine import FlowStopped
    from garuda.runtime.capacity import CapacityUnavailable
    from garuda.workspace.lease import LeaseConflictError

    if isinstance(exc, LeaseConflictError):
        return "workspace.busy", True
    if isinstance(exc, CapacityUnavailable):
        return "capacity.unavailable", True
    if isinstance(exc, FlowStopped):
        code = getattr(exc, "code", None)
        return (code if isinstance(code, str) and _REASON.fullmatch(code)
                else "flow.stopped"), True
    return "flow.failed", False


async def _settled_call(call, *args, **kwargs):
    """Wait for a bounded worker even when the gateway task is cancelled."""
    task = asyncio.create_task(asyncio.to_thread(call, *args, **kwargs))
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    value = task.result()
    if cancelled:
        raise asyncio.CancelledError
    return value


class FlowStartGateway:
    """Own admitted flow tasks independently of their HTTP connections."""

    def __init__(self, *, store: SessionStore, workspaces: tuple[Path, ...],
                 max_permission: str, approvals, admission_wait: float = 5.0):
        self.store = store
        self.workspaces = tuple(Path(path).resolve() for path in workspaces)
        self.max_permission = max_permission
        self.approvals = approvals
        self.owner = current_owner()
        self.server_epoch = self.owner.epoch
        self.backend_available = sys.platform == "linux"
        self.admission_wait = admission_wait
        self.intents = LaunchIntentStore(store.root)
        self._tasks: dict[str, asyncio.Task] = {}
        self._admissions: dict[str, asyncio.Future] = {}
        self._closing = False

    @property
    def available(self) -> bool:
        return self.backend_available and not self._closing

    def authority(self, plan, context) -> dict:
        return start_authority(
            plan, context, max_permission=self.max_permission,
            server_epoch=self.server_epoch, backend_available=self.backend_available,
            approval_available=self.approvals is not None)

    def preview(self, starter_id: str, inputs: dict, workspace: Path) -> dict:
        plan, context = compile_with_context(
            starter_id, inputs, workspace, store=self.store)
        return {"plan": plan.to_dict(),
                "readiness": readiness(context, starter_id=starter_id, owned=True),
                "configuration": setup_view.configuration(context.resolved),
                "authority": self.authority(plan, context)}

    def _workspace(self, index: int) -> tuple[Path, list[int]]:
        if type(index) is not int or not 0 <= index < len(self.workspaces):
            raise FlowStartError("starter.workspace_invalid",
                                 "workspace must select a configured workspace.", status=400)
        root = self.workspaces[index]
        try:
            resolved = root.resolve(strict=True)
            info = os.stat(resolved)
        except OSError as exc:
            raise FlowStartError("starter.workspace_invalid",
                                 "the configured workspace is unavailable.") from exc
        if resolved != root or not root.is_dir():
            raise FlowStartError("starter.workspace_invalid",
                                 "the configured workspace changed; restart the dashboard.")
        return root, [info.st_dev, info.st_ino]

    @staticmethod
    def _request_digest(spec: FlowStartRequest, root: Path, identity: list[int]) -> str:
        return digest({"version": 1, "starter_id": spec.starter_id,
                       "inputs": spec.inputs, "workspace": str(root),
                       "workspace_identity": identity,
                       "preview_digest": spec.preview_digest,
                       "authority_digest": spec.authority_digest})

    @staticmethod
    def _same_request(record: dict, spec: FlowStartRequest, request_digest: str,
                      root: Path, identity: list[int]) -> bool:
        return (record["request_digest"] == request_digest
                and record["plan_digest"] == spec.preview_digest
                and record["authority_digest"] == spec.authority_digest
                and record["workspace"] == str(root)
                and record["workspace_identity"] == identity)

    async def admit(self, spec: FlowStartRequest) -> dict:
        if self._closing:
            raise FlowStartError("starter.gateway_closing",
                                 "The dashboard is shutting down.", status=503)
        root, identity = self._workspace(spec.workspace)
        request_digest = self._request_digest(spec, root, identity)
        try:
            await _settled_call(self._ensure_store_root)
            existing = await _settled_call(self.intents.read, spec.operation_id)
        except (StorageError, OSError, ValueError) as exc:
            raise FlowStartError("starter.operation_unavailable",
                                 "The launch operation store is unavailable.", status=503) from exc
        if existing is not None:
            if not self._same_request(existing, spec, request_digest, root, identity):
                raise FlowStartError("starter.operation_conflict",
                                     "operation_id already names another launch request.")
            return await self._project(existing, duplicate=True)
        if not self.available:
            raise FlowStartError("starter.ownership_unavailable",
                                 "Owned starter runs require the Linux descendant supervisor.",
                                 status=503)
        try:
            plan, context = await _settled_call(
                compile_with_context, spec.starter_id, spec.inputs, root, store=self.store)
        except RESOLUTION_ERRORS as exc:
            raise FlowStartError(getattr(exc, "code", "starter.preview_changed"), str(exc),
                                 status=409) from exc
        if plan.provenance.get("workspace_identity") != identity:
            raise FlowStartError(
                "starter.workspace_changed",
                "The selected workspace changed during compilation; preview again.")
        if plan.digest != spec.preview_digest:
            raise FlowStartError("starter.preview_changed",
                                 "Starter inputs, sources or configuration changed; preview again.")
        authority = self.authority(plan, context)
        if authority["digest"] != spec.authority_digest:
            raise FlowStartError("starter.authority_changed",
                                 "Dashboard authority changed; preview again.")
        state = readiness(context, starter_id=plan.starter_id, owned=True)
        if not state["can_run"]:
            raise FlowStartError("starter.not_ready",
                                 "Resolve the starter readiness diagnostics before starting.")
        if not authority["can_start"]:
            first = authority["diagnostics"][0]
            raise FlowStartError(first["code"], first["message"])
        try:
            record, created = await _settled_call(
                self.intents.prepare, operation_id=spec.operation_id,
                request_digest=request_digest, plan_digest=plan.digest,
                authority_digest=authority["digest"], workspace=str(root),
                workspace_identity=identity, compiler_version=COMPILER_VERSION,
                catalog_version=CATALOG_VERSION, owner=self.owner,
                background=plan.kind == "run" and bool(plan.options.get("bg")))
        except IntentConflict as exc:
            raise FlowStartError("starter.operation_conflict", str(exc)) from exc
        except (StorageError, IntentStateError, OSError, ValueError) as exc:
            raise FlowStartError("starter.operation_unavailable",
                                 "The launch operation could not be durably prepared.",
                                 status=503) from exc
        if not created:
            if not self._same_request(record, spec, request_digest, root, identity):
                raise FlowStartError("starter.operation_conflict",
                                     "operation_id already names another launch request.")
            return await self._project(record, duplicate=True)

        admission = asyncio.get_running_loop().create_future()
        self._admissions[spec.operation_id] = admission
        task = asyncio.create_task(
            self._execute(record, plan, context),
            name=f"starter-flow-{spec.operation_id}")
        self._tasks[spec.operation_id] = task
        task.add_done_callback(lambda _task, operation=spec.operation_id:
                               self._tasks.pop(operation, None))
        try:
            payload = await asyncio.wait_for(asyncio.shield(admission), self.admission_wait)
        except TimeoutError:
            payload = await self._project(record, duplicate=False)
        return {**payload, "new": True}

    def _ensure_store_root(self) -> None:
        self.store.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = os.lstat(self.store.root)
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) & 0o022):
            raise OSError("session store root is not privately owned")

    async def _execute(self, record: dict, plan, context) -> None:
        operation = record["operation_id"]
        admission = self._admissions[operation]
        current = record
        try:
            current = await _settled_call(
                self.intents.advance, operation, expected="prepared",
                phase="launch-authorized", owner=self.owner)
            approval = self.approvals.make_handler(operation, lambda: None)

            async def admitted(session_id: str, lease_epoch: str) -> None:
                nonlocal current
                current = await _settled_call(
                    self.intents.advance, operation, expected="launch-authorized",
                    phase="admitted", owner=self.owner, session_id=session_id,
                    lease_epoch=lease_epoch)
                self._resolve_admission(admission, await self._project(current))

            binding = {
                "version": 1, "operation_id": operation,
                "request_digest": record["request_digest"],
                "plan_digest": record["plan_digest"],
                "authority_digest": record["authority_digest"],
                "workspace_identity": record["workspace_identity"],
                "owner": record["owner"],
            }
            if record["version"] == 2:
                from garuda.interfaces import bg_sessions
                from garuda.runtime.queue import QueueStore
                from garuda.scenarios.background import capsule

                queue = QueueStore()
                request = capsule(plan, context, binding, self.max_permission, queue)
                await _settled_call(
                    bg_sessions.launch, store=self.store, queue=queue,
                    reserved_session=record["reserved_session"], operation_binding=binding,
                    owned_request=request)
                current = await _settled_call(self.intents.read, operation)
                self._resolve_admission(admission, await self._project(current))
                return
            if plan.kind == "run":
                from garuda.agents.role_runs import RoleRunService

                result = await RoleRunService(self.store).run_prepared(
                    plan, context, run_session=record["reserved_session"],
                    operation_binding=binding, on_admitted=admitted,
                    approval_handler=approval, permission_mode=self.max_permission)
            else:
                from garuda.flows.service import FlowExecutionService

                result = await FlowExecutionService(self.store).run_prepared(
                    plan, context, flow_session=record["reserved_session"],
                    operation_binding=binding, on_admitted=admitted,
                    approval_handler=approval, permission_mode=self.max_permission,
                    checks=() if plan.starter_id == "build-review" else None)
            if current["phase"] == "admitted":
                meta = await _settled_call(self.store.load_meta, result.flow.flow_session)
                if meta.get("flow_state") == "quarantined":
                    current = await _settled_call(
                        self.intents.advance, operation, expected="admitted",
                        phase="quarantined", owner=self.owner,
                        reason="flow.quarantined")
                else:
                    outcome = "completed" if result.flow.completed else "stopped"
                    current = await _settled_call(
                        self.intents.advance, operation, expected="admitted", phase="terminal",
                        owner=self.owner, outcome=outcome)
            self._resolve_admission(admission, await self._project(current))
        except asyncio.CancelledError:
            current = await self._settle_interruption(current, reason="server.shutdown")
            self._resolve_admission(admission, await self._project(current))
            raise
        except Exception as exc:
            logger.exception("Owned starter flow %s failed", operation)
            reason, refusal = _failure_reason(exc)
            current = await self._settle_interruption(
                current, reason=reason, refusal=refusal)
            self._resolve_admission(admission, await self._project(current))
        finally:
            self.approvals.forget(operation)
            self._admissions.pop(operation, None)

    @staticmethod
    def _resolve_admission(future: asyncio.Future, value: dict) -> None:
        if not future.done():
            future.set_result(value)

    async def _settle_interruption(self, record: dict, *, reason: str,
                                   refusal: bool = False) -> dict:
        try:
            latest = await _settled_call(self.intents.read, record["operation_id"])
            if latest is None:
                return {**record, "phase": "uncertain", "reason": "publication.uncertain"}
            record = latest
            phase = record["phase"]
            if record["version"] == 2 and record.get("queue_admission") is not None:
                return record  # worker lifetime is independent of this server
            if phase == "prepared":
                return await _settled_call(
                    self.intents.advance, record["operation_id"], expected=phase,
                    phase="abandoned", owner=self.owner, reason=reason)
            meta = await self._bound_meta(record)
            if phase == "launch-authorized" and meta is not None:
                binding = meta["flow_operation"]
                record = await _settled_call(
                    self.intents.advance, record["operation_id"], expected=phase,
                    phase="admitted", owner=self.owner,
                    session_id=record["reserved_session"],
                    lease_epoch=binding["lease_epoch"])
                phase = "admitted"
            if phase == "admitted":
                meta = meta or await self._bound_meta(record)
                flow_state = meta.get("flow_state") if meta else None
                if flow_state == "quarantined":
                    return await _settled_call(
                        self.intents.advance, record["operation_id"], expected=phase,
                        phase="quarantined", owner=self.owner,
                        reason="flow.quarantined")
                if flow_state in {"completed", "stopped", "interrupted"}:
                    outcome = ("completed" if flow_state == "completed" else
                               "stopped" if flow_state == "stopped" else "interrupted")
                    return await _settled_call(
                        self.intents.advance, record["operation_id"], expected=phase,
                        phase="terminal", owner=self.owner, outcome=outcome)
                return await _settled_call(
                    self.intents.advance, record["operation_id"], expected=phase,
                    phase="uncertain", owner=self.owner, reason="publication.uncertain")
            if phase == "launch-authorized":
                # A pre-launch refusal plus an absent reserved parent proves
                # nothing launched: the runner reserves the parent only after it
                # holds the lease, no child precedes it, and it has unwound here.
                # Cancellation and unknown failures stay uncertain.
                reserved = self.store.session_dir(record["reserved_session"])
                if refusal and not await _settled_call(os.path.lexists, reserved):
                    return await _settled_call(
                        self.intents.advance, record["operation_id"], expected=phase,
                        phase="refused", owner=self.owner, reason=reason)
                return await _settled_call(
                    self.intents.advance, record["operation_id"], expected=phase,
                    phase="uncertain", owner=self.owner, reason=reason)
            return record
        except (StorageError, IntentStateError, OSError, ValueError, KeyError):
            return {**record, "phase": "uncertain", "reason": "publication.uncertain"}

    async def _bound_meta(self, record: dict) -> dict | None:
        session_id = record.get("session_id") or record.get("reserved_session")
        try:
            meta = await _settled_call(self.store.load_meta, session_id)
        except (FileNotFoundError, OSError, ValueError, StorageError):
            return None
        binding = meta.get("flow_operation")
        policy = meta.get("publication_policy")
        starter = meta.get("starter")
        execution = meta.get("flow_execution_workspace")
        if execution is None:
            workspace_matches = meta.get("workspace") == record["workspace"]
        else:
            workspace_matches = (
                isinstance(execution, dict) and set(execution) == {"source", "path", "identity"}
                and execution.get("source") == record["workspace"]
                and execution.get("path") == meta.get("workspace") == meta.get("worktree")
                and meta.get("source_repo") == record["workspace"]
                and meta.get("isolation") == "worktree"
                and type(execution.get("identity")) is list and len(execution["identity"]) == 2
                and all(type(part) is int and part >= 0 for part in execution["identity"]))
        if (not isinstance(binding, dict)
                or set(binding) != {"version", "operation_id", "request_digest",
                                    "plan_digest", "authority_digest", "workspace_identity", "owner",
                                    "session_id", "lease_epoch"}
                or binding.get("version") != 1
                or binding.get("operation_id") != record["operation_id"]
                or binding.get("request_digest") != record["request_digest"]
                or binding.get("plan_digest") != record["plan_digest"]
                or binding.get("authority_digest") != record["authority_digest"]
                or binding.get("workspace_identity") != record["workspace_identity"]
                or binding.get("owner") != record["owner"]
                or binding.get("session_id") != record["reserved_session"]
                or not isinstance(binding.get("lease_epoch"), str)
                or meta.get("session_id") != record["reserved_session"]
                or not workspace_matches
                # Checked starts publish under the strict verification policy;
                # both kinds bind the same operation through ``flow_operation``.
                or not isinstance(policy, dict)
                or policy.get("kind") not in {"owned-flow", "flow-verification"}
                or policy.get("session_id") != record["reserved_session"]
                or not isinstance(starter, dict)
                or starter.get("plan_digest") != record["plan_digest"]):
            return None
        if record.get("lease_epoch") is not None and binding["lease_epoch"] != record["lease_epoch"]:
            return None
        return meta

    async def _project(self, record: dict, *, duplicate: bool = False) -> dict:
        if record["version"] == 2 and record["phase"] != "prepared":
            from garuda.scenarios.background import project

            return await _settled_call(project, self.store, record, duplicate=duplicate)
        phase = record["phase"]
        task = self._tasks.get(record["operation_id"])
        active = task is not None and not task.done()
        owner_live = owner_liveness(record["owner"])
        payload = {"operation_id": record["operation_id"], "phase": phase,
                   "session_id": record.get("session_id"),
                   "outcome": record.get("outcome"), "reason": record.get("reason"),
                   "duplicate": duplicate}
        if phase == "prepared":
            payload["status"] = "admission-pending" if active else (
                "abandoned" if owner_live is False else "uncertain")
            return payload
        if phase == "launch-authorized":
            meta = await self._bound_meta(record)
            if meta is not None:
                self._project_bound(payload, meta, durable_terminal=False)
                if (payload["status"] == "admitted" and not active
                        and owner_live is not True):
                    payload.update(status="uncertain", reason="owner.uncertain")
            else:
                payload["status"] = "admission-pending" if active else "uncertain"
            return payload
        if phase in {"admitted", "terminal"}:
            meta = await self._bound_meta(record)
            if meta is None:
                payload.update(status="uncertain", reason="publication.uncertain")
                return payload
            self._project_bound(payload, meta, durable_terminal=phase == "terminal")
            if phase == "terminal" and payload["status"] != "terminal":
                payload.update(status="uncertain", reason="publication.uncertain")
            elif (payload["status"] == "admitted" and not active
                  and owner_live is not True):
                payload.update(status="uncertain", reason="owner.uncertain")
            return payload
        payload["status"] = phase
        return payload

    @staticmethod
    def _project_bound(payload: dict, meta: dict, *, durable_terminal: bool) -> None:
        flow_state = meta.get("flow_state")
        payload.update(session_id=meta["session_id"], flow_state=flow_state)
        if flow_state == "quarantined":
            payload.update(status="quarantined", reason="flow.quarantined")
        elif flow_state in {"completed", "stopped", "interrupted"}:
            payload["status"] = "terminal"
            payload["outcome"] = ("completed" if flow_state == "completed" else
                                  "stopped" if flow_state == "stopped" else "interrupted")
        elif durable_terminal:
            payload.update(status="uncertain", reason="publication.uncertain")
        else:
            payload["status"] = "admitted"

    async def status(self, operation_id: str) -> dict | None:
        try:
            record = await _settled_call(self.intents.read, operation_id)
        except ValueError as exc:
            raise FlowStartError("starter.operation_invalid", str(exc), status=400) from exc
        except (StorageError, OSError) as exc:
            raise FlowStartError("starter.operation_unavailable",
                                 "The launch operation store is unavailable.", status=503) from exc
        if record is None:
            return None
        root, identity = self._workspace_for_record(record)
        if record["workspace"] != str(root) or record["workspace_identity"] != identity:
            raise FlowStartError("starter.operation_scope_changed",
                                 "The operation workspace is no longer authorized.", status=403)
        payload = await self._project(record)
        if payload["status"] in {"admission-pending", "admitted"}:
            self.approvals.touch(operation_id)
        return payload

    def _workspace_for_record(self, record: dict) -> tuple[Path, list[int]]:
        for root in self.workspaces:
            if str(root) == record.get("workspace"):
                try:
                    resolved = root.resolve(strict=True)
                    info = os.stat(resolved)
                except OSError as exc:
                    raise FlowStartError("starter.operation_scope_changed",
                                         "The operation workspace is unavailable.",
                                         status=403) from exc
                if resolved != root or not root.is_dir():
                    raise FlowStartError(
                        "starter.operation_scope_changed",
                        "The operation workspace identity changed.", status=403)
                return root, [info.st_dev, info.st_ino]
        raise FlowStartError("starter.operation_scope_changed",
                             "The operation workspace is outside this dashboard allowlist.",
                             status=403)

    async def aclose(self) -> None:
        self._closing = True
        tasks = list(self._tasks.items())
        for operation, task in tasks:
            self.approvals.forget(operation)
            task.cancel()
        if tasks:
            await asyncio.gather(*(task for _operation, task in tasks),
                                 return_exceptions=True)
