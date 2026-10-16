"""Shared flow orchestration for the CLI and other in-process callers.

The engine owns admission, workspace leases, step receipts and recovery.
This facade resolves the effective flow and refuses missing roles before
passing execution to that owner. Its opt-in check path freezes authority before
admission; the engine remains the sole acceptance owner.
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
                  starter_record: dict | None = None,
                  checks: tuple[str, ...] | None = None) -> FlowExecutionResult:
        return await self._execute(name, task, workspace, starter_record=starter_record,
                                   checks=checks)

    async def resume(self, flow_session: str) -> FlowExecutionResult:
        meta = self.store.load_meta(flow_session)
        if meta.get("publication_policy") is not None:
            from garuda.flows.verification_records import FlowVerificationRecords
            from garuda.runtime.strict_store import StorageError

            records = FlowVerificationRecords(self.store.session_dir(flow_session), flow_session)
            reference = meta.get("flow_verification_commit")
            if reference is not None:
                try:
                    commit = records.committed(
                        reference, meta.get("acceptance_receipts"),
                        meta.get("flow_verification_phase"))
                except (StorageError, OSError, ValueError) as exc:
                    raise engine.FlowStopped(
                        "flow.verification_reconciliation_required",
                        "protected flow publication disagrees with its durable check records; "
                        "steps and checks are never replayed automatically") from exc
                state = meta.get("state")
                evidence = meta.get("flow_evidence_manifest")
                if (meta.get("flow_state") != "completed"
                        or meta.get("status") != "completed"
                        or not isinstance(evidence, dict)
                        or evidence.get("manifest_digest") != commit["evidence_digest"]
                        or not isinstance(state, dict)
                        or state.get("verification") != commit["verification"]):
                    raise engine.FlowStopped(
                        "flow.verification_reconciliation_required",
                        "protected flow final metadata is not bound to its check commitment; "
                        "steps and checks are never replayed automatically")
                return FlowExecutionResult(
                    engine.FlowResult(flow_session, engine.receipts(self.store, flow_session)),
                    meta.get("review"), meta.get("state"))
            try:
                recovery = records.reconcile_unfinished()
            except (StorageError, OSError, ValueError) as exc:
                raise engine.FlowStopped(
                    "flow.verification_reconciliation_required",
                    "protected flow check records are missing or corrupt; checks are never "
                    "replayed automatically") from exc
            classification = recovery["classification"]
            code = ("flow.verification_uncertain" if classification == "uncertain" else
                    "flow.verification_publication_incomplete" if classification in {
                        "commit-unpublished", "terminal-unpublished"} else
                    "flow.verification_interrupted")
            states = ", ".join(
                f"{row['index']}:{row['classification']}" for row in recovery["checks"])
            raise engine.FlowStopped(
                code, f"protected flow check phase is {classification}"
                      f"{f' ({states})' if states else ''}; checks are never replayed automatically")
        return await self._execute(meta["flow"]["name"], meta["task"], meta["workspace"],
                                   flow_session=flow_session)

    async def _execute(self, name: str, task: str, workspace: str, *,
                       flow_session: str | None = None,
                       starter_record: dict | None = None,
                       checks: tuple[str, ...] | None = None) -> FlowExecutionResult:
        from garuda.flows.launch import launch_step

        request = permissions = None
        if checks is None:
            resolved = load_effective(workspace)
        else:
            from garuda.config_sources import SourceCapture
            from garuda.core.acceptance_request import freeze_resolved_checks
            from garuda.core.permissions import PermissionEngine

            if (not isinstance(checks, tuple) or len(checks) > 16
                    or any(not isinstance(check, str) or not check.strip()
                           or len(check) > 2048 or "\0" in check for check in checks)):
                raise engine.FlowStopped("flow.check_request_invalid",
                                         "select at most 16 bounded check commands")
            permissions = PermissionEngine(mode="smart")
            with SourceCapture() as capture:
                resolved = load_effective(workspace, cli_checks=checks)
            request = freeze_resolved_checks(resolved, workspace, capture.facts(), permissions)
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
        if request is not None and request.checks:
            import sys

            if sys.platform != "linux":
                raise engine.FlowStopped(
                    "flow.verification_unavailable",
                    "owned flow checks require the Linux supervisor on this host")
        else:
            request = permissions = None
        runner = engine.FlowRunner(self.store, workspace, name, flow, resolved, task=task,
                                   launcher=launch_step, flow_session=flow_session,
                                   starter_record=starter_record,
                                   verification_request=request,
                                   verification_permissions=permissions)
        result = await runner.run(resume=flow_session is not None)
        meta = self.store.load_meta(result.flow_session)
        return FlowExecutionResult(result, meta.get("review"), meta.get("state"))
