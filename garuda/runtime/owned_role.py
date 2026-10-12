"""Shared Linux-owned native and ACP execution for callers retaining a parent lease."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path

from garuda.config_sources import SourceCapture, SourceFact, record_digest
from garuda.runtime.process_supervision import CleanupUncertain, LinuxProcessSupervisor, ProcessRequest
from garuda.runtime.role_control import RoleControl
from garuda.runtime.role_request import encode_document, freeze_acp, freeze_native, freeze_role


@dataclass(frozen=True)
class OwnedRoleRequest:
    """Already resolved role values. The JSON contains no lease authority."""

    workspace: str
    capacity_key: str
    payload: str
    session_id: str
    timeout: float = 3600
    sources: tuple[SourceFact, ...] = ()

    @property
    def digest(self):
        return hashlib.sha256(encode_document({"payload": self.payload, "sources": [asdict(source) for source in self.sources]}).encode()).hexdigest()


def prepare_owned_step(launch, *, timeout=3600) -> OwnedRoleRequest:
    with SourceCapture() as capture:
        if launch.role_plan is not None and launch.role_plan.agent_spec is not None:
            for source in launch.role_plan.agent_spec.resolved().chain:
                if source.path is not None:
                    record_digest(source.path, source.digest)
        request = _prepare_owned_step(launch, timeout=timeout)
        return OwnedRoleRequest(request.workspace, request.capacity_key, request.payload,
                                request.session_id, request.timeout, capture.facts())


def _prepare_owned_step(launch, *, timeout) -> OwnedRoleRequest:
    from garuda.agents import role_agent
    from garuda.agents.setup import plan_agent_run, prepare_runtime_catalog
    from garuda.core.sessions import SessionStore

    workspace = os.path.realpath(launch.workspace)
    role = launch.role_plan
    store = launch.store or SessionStore()
    session_id = str(uuid.uuid4())
    payload = {"version": 1, "workspace": workspace, "prompt": launch.prompt,
               "session_id": session_id, "sessions_root": str(store.root.resolve()),
               "role": freeze_role(role), "no_edits": launch.no_edits,
               "flow": {"flow_session": launch.flow_session, "step": launch.step_id,
                        "attempt": launch.attempt}}
    if role is not None and role.kind == "acp":
        manifest = freeze_acp(prepare_runtime_catalog(workspace), role.runtime_id)
        payload.update(kind="acp", manifest=manifest)
        capacity_key = manifest["runtime_id"]
    else:
        native = plan_agent_run(
            role_agent.native_spec(role, workspace) if role and role.profile else "build",
            workspace=workspace, model=role.model_id if role else None,
            permission_mode="readonly" if launch.no_edits else (role.permissions if role else None),
            reasoning_effort=role.effort if role else None)
        payload.update(kind="native", native=freeze_native(native))
        capacity_key = "native"
    return OwnedRoleRequest(workspace, capacity_key, encode_document(payload), session_id, timeout)


async def execute_owned_role(request: OwnedRoleRequest, capability, *, approval=None):
    """Return a step only after the actual worker and all descendants are reaped.

    The issued borrowed guard remains parent-owned. Cancellation closes the
    subtree before returning; lost closure or an incomplete normal result
    quarantines the same parent and child reservation.
    """
    from garuda.flows.engine import StepResult

    payload = json.loads(request.payload)
    if (payload.get("workspace") != request.workspace or payload.get("session_id") != request.session_id
            or request.capacity_key != ("native" if payload.get("kind") == "native"
                                        else payload["manifest"]["runtime_id"])):
        raise ValueError("owned role request binding differs")
    control = RoleControl(capability, request.workspace, request.capacity_key, approval=approval)
    from garuda.core.acceptance_work import owned_call

    async def validate_sources():
        await owned_call("admitted-sources", {"sources": [asdict(source) for source in request.sources]})

    await validate_sources()
    process = await LinuxProcessSupervisor().prepare()
    started = False
    normal_result = False
    try:
        endpoint = await control.start(process)
        started = True
        bootstrap = ("import runpy,sys;sys.path.insert(0,sys.argv[1]);"
                     "runpy.run_module('garuda.runtime.role_worker',run_name='__main__')")
        command = (sys.executable, "-I", "-c", bootstrap, str(Path(__file__).resolve().parents[2]))
        execution = ProcessRequest(command, request.workspace, timeout=request.timeout,
                                   input_data=encode_document({"request": payload, "control": endpoint}),
                                   purpose="role")
        await control.guard.race(process.activate(execution))
        control.activated()
        terminal = await control.guard.race(process.wait())
        await validate_sources()
        result = control.result
        if terminal.outcome != "exited" or result is None or result["session_id"] != request.session_id:
            raise CleanupUncertain("owned role closed without a complete bound result")
        closure = {"version": 1, "request_digest": request.digest,
                   "session_id": request.session_id, "process": asdict(terminal)}
        normal_result = True
        return StepResult(result["session_id"], bool(result["success"] and terminal.exit_code == 0),
                          result["output"], closure=closure)
    except asyncio.CancelledError:
        await process.close()
        # ECHILD is sufficient for cancellation closure; no successful result
        # is claimed. The flow's phase owner records cancellation separately.
        normal_result = process.result is not None
        raise
    finally:
        control.activated()
        try:
            await process.close()
        finally:
            if started:
                await control.close(closed=normal_result and process.result is not None)


async def launch_owned_step(launch):
    """Flow adapter for the owned role facade; public verification is enabled separately."""
    request = prepare_owned_step(launch)
    return await execute_owned_role(request, launch.capability)
