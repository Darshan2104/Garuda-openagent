"""Shared flow orchestration for the CLI and other in-process callers.

The engine owns admission, workspace leases, step receipts and recovery.
This facade resolves the effective flow and refuses missing roles before
passing execution to that owner. It does not run acceptance checks.
"""

from __future__ import annotations

from dataclasses import dataclass

from garuda.config.garuda_yaml import load_effective
from garuda.core.sessions import SessionStore
from garuda.flows import engine, packaged


@dataclass(frozen=True)
class FlowExecutionResult:
    """Engine outcome and recorded review/state evidence; no inferred verification."""

    flow: engine.FlowResult
    review: dict | None
    state: dict | None


class FlowExecutionService:
    def __init__(self, store: SessionStore | None = None):
        self.store = store if store is not None else SessionStore()

    async def run(self, name: str, task: str, workspace: str, *,
                  starter_record: dict | None = None) -> FlowExecutionResult:
        return await self._execute(name, task, workspace, starter_record=starter_record)

    async def resume(self, flow_session: str) -> FlowExecutionResult:
        meta = self.store.load_meta(flow_session)
        if meta.get("publication_policy") is not None:
            from garuda.flows.verification_records import FlowVerificationRecords
            from garuda.runtime.strict_store import StorageError

            try:
                commit = FlowVerificationRecords(self.store.session_dir(flow_session),
                                                 flow_session).committed(
                                                     meta.get("flow_verification_commit"))
                state = meta.get("state")
                if (meta.get("flow_state") != "completed"
                        or meta.get("status") != "completed"
                        or not isinstance(state, dict)
                        or state.get("verification") != commit["verification"]):
                    raise ValueError("protected flow final state is not committed")
            except (StorageError, OSError, ValueError) as exc:
                raise engine.FlowStopped(
                    "flow.verification_reconciliation_required",
                    "protected flow has incomplete or uncertain verification; "
                    "steps and checks are never replayed automatically") from exc
            return FlowExecutionResult(
                engine.FlowResult(flow_session, engine.receipts(self.store, flow_session)),
                meta.get("review"), meta.get("state"))
        return await self._execute(meta["flow"]["name"], meta["task"], meta["workspace"],
                                   flow_session=flow_session)

    async def _execute(self, name: str, task: str, workspace: str, *,
                       flow_session: str | None = None,
                       starter_record: dict | None = None) -> FlowExecutionResult:
        from garuda.flows.launch import launch_step

        resolved = load_effective(workspace)
        flows = packaged.available(resolved)
        if name not in flows:
            raise engine.FlowStopped("flow.unknown", f"no flow named {name!r} "
                                     f"(have: {', '.join(sorted(flows))})")
        flow, _source = flows[name]
        missing = packaged.missing_roles(flow, resolved)
        if missing:
            raise engine.FlowStopped(
                "flow.missing_roles", f"flow {name} needs the roles "
                f"{', '.join(packaged.required_roles(flow))}; define "
                f"{', '.join(missing)} in garuda.yaml (`garuda init` proposes them)")
        runner = engine.FlowRunner(self.store, workspace, name, flow, resolved, task=task,
                                   launcher=launch_step, flow_session=flow_session,
                                   starter_record=starter_record)
        result = await runner.run(resume=flow_session is not None)
        meta = self.store.load_meta(result.flow_session)
        return FlowExecutionResult(result, meta.get("review"), meta.get("state"))
