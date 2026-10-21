"""Shared flow orchestration for the CLI and other in-process callers.

The engine owns admission, workspace leases, step receipts and recovery.
This facade resolves the effective flow and refuses missing roles before
passing execution to that owner. Its opt-in check path freezes authority before
admission; the engine remains the sole acceptance owner.
"""

from __future__ import annotations

import re
import uuid
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

    async def run_prepared(self, plan, context, *, flow_session: str,
                           operation_binding: dict, on_admitted, approval_handler,
                           permission_mode: str,
                           checks: tuple[str, ...] | None = None) -> FlowExecutionResult:
        """Execute the exact starter objects admitted by the dashboard gateway.

        This path never reloads the flow, role bindings or runtime registry. The
        caller has already persisted launch authorization; the engine still owns
        the workspace lease, strict parent publication and every child lifetime.
        """
        from garuda.scenarios.compile import LaunchContext
        from garuda.scenarios.types import LaunchPlan

        if (not isinstance(plan, LaunchPlan) or not isinstance(context, LaunchContext)
                or plan.kind != "flow" or context.kind != "flow"
                or context.flow is None or plan.flow != context.flow
                or plan.target != context.target):
            raise engine.FlowStopped("flow.prepared_invalid",
                                     "the admitted starter plan and flow context disagree")
        return await self._run_owned(
            plan, context, flow=context.flow, name=plan.target, flow_session=flow_session,
            operation_binding=operation_binding, on_admitted=on_admitted,
            approval_handler=approval_handler, permission_mode=permission_mode,
            checks=checks)

    async def _run_owned(self, plan, context, *, flow: dict, name: str, flow_session: str,
                         operation_binding: dict, on_admitted, approval_handler,
                         permission_mode: str, checks: tuple[str, ...] | None,
                         launch_kind: str = "flow",
                         session_name: str | None = None, background_admission=None) -> FlowExecutionResult:
        """The one owned parent path for dashboard flow and role-run starts."""
        import sys

        from garuda.flows.launch import launch_step

        binding_fields = {"version", "operation_id", "request_digest", "plan_digest",
                          "authority_digest", "workspace_identity", "owner"}
        owner = operation_binding.get("owner") if isinstance(operation_binding, dict) else None
        try:
            operation_uuid = uuid.UUID(operation_binding.get("operation_id"))
            session_uuid = uuid.UUID(flow_session)
        except (ValueError, TypeError, AttributeError):
            operation_uuid = session_uuid = None
        if (plan.bindings != {name: role.record()
                             for name, role in context.role_plans.items()}
                or plan.provenance.get("fallback_candidates", {}) != {
                    name: [candidate.record() for candidate in choices]
                    for name, choices in context.fallback_plans.items()}
                or not isinstance(operation_binding, dict)
                or set(operation_binding) != binding_fields
                or operation_binding.get("version") != 1
                or operation_binding.get("plan_digest") != plan.digest
                or operation_binding.get("workspace_identity")
                != plan.provenance.get("workspace_identity")
                or type(operation_binding.get("workspace_identity")) is not list
                or len(operation_binding["workspace_identity"]) != 2
                or any(type(part) is not int or part < 0
                       for part in operation_binding["workspace_identity"])
                or operation_uuid is None or operation_uuid.version != 4
                or str(operation_uuid) != operation_binding.get("operation_id")
                or session_uuid is None or session_uuid.version != 4
                or str(session_uuid) != flow_session
                or any(not isinstance(operation_binding.get(name), str)
                       or re.fullmatch(r"[0-9a-f]{64}", operation_binding[name]) is None
                       for name in ("request_digest", "plan_digest", "authority_digest"))
                or not isinstance(owner, dict)
                or set(owner) != {"pid", "identity", "pgid", "epoch"}
                or type(owner.get("pid")) is not int or owner["pid"] <= 0
                or type(owner.get("pgid")) is not int or owner["pgid"] <= 0
                or not isinstance(owner.get("identity"), str) or not owner["identity"]
                or not isinstance(owner.get("epoch"), str)
                or re.fullmatch(r"[0-9a-f]{32}", owner["epoch"]) is None
                or not callable(on_admitted) or not callable(approval_handler)):
            raise engine.FlowStopped("flow.prepared_invalid",
                                     "the admitted role or operation binding disagrees")
        if permission_mode not in {"readonly", "smart", "auto", "yolo"}:
            raise engine.FlowStopped("flow.permission_ceiling_invalid",
                                     "the dashboard permission ceiling is unsupported")
        if sys.platform != "linux":
            raise engine.FlowStopped("flow.ownership_unavailable",
                                     "owned dashboard flows require the Linux supervisor")
        from garuda.scenarios.authority import start_authority

        authority = start_authority(
            plan, context, max_permission=permission_mode,
            server_epoch=owner["epoch"], backend_available=True,
            approval_available=True)
        if (not authority["can_start"]
                or authority["digest"] != operation_binding["authority_digest"]):
            raise engine.FlowStopped("flow.authority_invalid",
                                     "the prepared start exceeds or disagrees with its authority")

        if background_admission is not None:
            from garuda.scenarios.background import BackgroundAdmission

            if (type(background_admission) is not BackgroundAdmission
                    or background_admission.session_id != flow_session
                    or background_admission.operation["binding"] != operation_binding):
                raise engine.FlowStopped("flow.background_invalid", "background admission disagrees")
            background_admission.validate()
        elif plan.options.get("bg"):
            raise engine.FlowStopped("flow.background_invalid", "background runs require their queue owner")
        request = permissions = None
        if checks is not None:
            from garuda.core.acceptance_request import freeze_resolved_checks
            from garuda.core.permissions import PermissionEngine

            self._validate_checks(checks)
            permissions = PermissionEngine(mode=permission_mode)
            request = freeze_resolved_checks(
                context.resolved, plan.workspace, context.configuration_sources, permissions)
            if not request.checks:
                request = permissions = None

        from garuda.agents.run_workspace import run_workspace

        isolation = plan.options.get("isolation", "shared") if launch_kind == "run" else "shared"
        async with run_workspace(self.store, plan.workspace, flow_session, isolation,
                                 reserved_background=background_admission is not None) as selected:
            workspace, worktree, identity = selected
            execution = ({"source": plan.workspace, "path": workspace, "identity": identity}
                         if workspace != plan.workspace else None)
            runner = engine.FlowRunner(
                self.store, workspace, name, flow, context.resolved,
                task=plan.task, launcher=launch_step, flow_session=flow_session,
                starter_record=plan.launch_metadata(), verification_request=request,
                verification_permissions=permissions, prepared_roles=context.role_plans,
                runtime_catalog=context.catalog, authority_sources=context.source_facts,
                configuration_sources=context.configuration_sources,
                operation_binding=operation_binding, on_admitted=on_admitted,
                approval_handler=approval_handler, launch_kind=launch_kind,
                session_name=session_name, execution_workspace=execution, worktree_plan=worktree,
                prepared_fallbacks=context.fallback_plans, background_admission=background_admission)
            result = await runner.run()
        meta = self.store.load_meta(result.flow_session)
        return FlowExecutionResult(result, meta.get("review"), meta.get("state"))

    @staticmethod
    def _validate_checks(checks: tuple[str, ...]) -> None:
        if (not isinstance(checks, tuple) or len(checks) > 16
                or any(not isinstance(check, str) or not check.strip()
                       or len(check) > 2048 or "\0" in check for check in checks)):
            raise engine.FlowStopped("flow.check_request_invalid",
                                     "select at most 16 bounded check commands")

    async def resume(self, flow_session: str) -> FlowExecutionResult:
        meta = self.store.load_meta(flow_session)
        policy = meta.get("publication_policy")
        if isinstance(policy, dict) and policy.get("kind") == "owned-flow":
            raise engine.FlowStopped(
                "flow.operation_managed",
                "dashboard-owned flow operations are projected from their durable intent; "
                "they are never resumed automatically")
        if policy is not None:
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

            self._validate_checks(checks)
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
