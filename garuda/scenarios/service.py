"""Pure starter library, readiness and preview; execution is a separate owner."""

from __future__ import annotations

import shutil
from pathlib import Path

from garuda.acp.catalog import RuntimeSettingsError
from garuda.acp.login_probe import cached_login
from garuda.config import garuda_yaml as gy
from garuda.context.redact import redact_text
from garuda.core import setup_view
from garuda.core.acceptance import checks_with_authority
from garuda.core.sessions import SessionStore
from garuda.flows import review
from garuda.runtime.registry import RegistryError
from garuda.runtime.roles import RoleRefused, acp_options
from garuda.scenarios.catalog import load_catalog
from garuda.scenarios.compile import LaunchContext, compile_with_context, resolve_launch, workspace_path
from garuda.scenarios.inputs import validate_inputs
from garuda.scenarios.types import LaunchPlan, StarterError

RESOLUTION_ERRORS = (StarterError, gy.GarudaConfigError, RegistryError, RoleRefused, RuntimeSettingsError)
INDEPENDENCE_LIMIT = (
    "Configured preflight only: execution rechecks identities actually launched or consulted. "
    "Primary runtime aliases are canonical; fallback and consulted harness aliases are compared "
    "as configured strings until the review-engine follow-up (#310).")


def _diagnostic(code, message, fix, *, level="warning"):
    return {"code": code, "message": redact_text(str(message))[0], "fix": fix, "level": level}


def _refusal(exc) -> dict:
    code = getattr(exc, "code", "starter.resolution_not_checked")
    status = "not-checked" if isinstance(exc, (RegistryError, RuntimeSettingsError, RoleRefused)) else "needs-setup"
    return {"status": status, "can_run": False, "review_label": "not checked",
            "reviews": [], "bindings": {}, "remedies": [], "runtime_evidence": {},
            "diagnostics": [_diagnostic(code, str(exc), "Review `garuda config show` and rerun `garuda init`.", level="error")]}


def _independence_remedies(target: str) -> list[dict]:
    return [
        {"id": "second-harness", "title": "Connect a second harness", "command": "garuda init",
         "description": "Connect an independent reviewer with its own CLI, then rerun Garuda setup."},
        {"id": "build-and-check", "title": "Build and check", "starter": "run-with-role", "role": "coder",
         "review_label": "no review", "description": "Use the existing role run and trusted/explicit acceptance checks; this performs no review."},
        {"id": "user-waiver", "title": "Review that is not independent",
         "command": f"garuda config show --flow {target}", "review_label": "review not independent",
         "description": "Copy the effective flow into your garuda.yaml and explicitly author review.independent: false. Garuda never creates this waiver for you."},
    ]


def readiness(context: LaunchContext, *, starter_id: str) -> dict:
    """Configured prerequisites, with separately labelled unprobed runtime facts."""
    diagnostics, blockers, unchecked, evidence = [], [], [], {}
    registry = context.catalog.registry
    for name, plan in context.role_plans.items():
        manifest = registry.manifest_for(plan.runtime_id)
        declared = registry.get(context.resolved.config["roles"][name]["harness"]).capabilities
        row = {"runtime_id": plan.runtime_id, "model_id": plan.model_id,
               "model_label": plan.model_id or "harness default", "identity": "configured",
               "capabilities": {"declared": sorted(declared.names), "observed": "not checked"},
               "version": "not checked", "login": "not checked"}
        if plan.kind == "native":
            row["availability"] = "in-process"
        else:
            row["availability"] = "present" if manifest.command and shutil.which(manifest.command[0]) else "missing"
            if row["availability"] == "missing":
                blockers.append("harness.cli_missing")
                diagnostics.append(_diagnostic("harness.cli_missing", f"{name}: executable unavailable for {plan.runtime_id}",
                                               manifest.setup or "Install the harness's official CLI and rerun `garuda init`.", level="error"))
            if plan.permissions == "readonly":
                from garuda.workspace.confined_acp import Confinement

                confinement = Confinement.from_config(plan.harness)
                row["confinement"] = "configured; runtime preflight not checked" if confinement else "missing image"
                if confinement is None or not shutil.which("docker"):
                    blockers.append("workspace.readonly_unenforced")
                    diagnostics.append(_diagnostic(
                        "workspace.readonly_unenforced",
                        f"{name}: read-only ACP requires a configured confinement image and Docker executable",
                        "Configure harnesses.<runtime>.confinement.image in your user garuda.yaml and install Docker; runtime preflight must still prove confinement.",
                        level="error"))
            try:
                acp_options(plan, manifest.version)
            except RoleRefused as exc:
                unchecked.append(exc.code)
                diagnostics.append(_diagnostic(exc.code, str(exc), "Use a proven adapter or remove unsupported model/effort options.", level="error"))
            try:
                cached = cached_login(manifest)
            except (TypeError, ValueError):
                cached = None
            if cached:
                row["login"] = {"last_recorded": cached[0].value, "at": cached[1], "current": False}
            diagnostics.append(_diagnostic("harness.not_checked", f"{name}: version, offered options and current login checked only at launch",
                                           f"Run `garuda doctor --runtime {plan.runtime_id}` for an explicit check.", level="info"))
        evidence[name] = row

    reviews = []
    for step in (context.flow or {}).get("steps", []):
        if "review" not in step:
            continue
        spec, reviewed = step["review"], step["role"]
        reviewer = spec["by"]
        reason = review.check_independent(context.resolved.config, reviewed, context.role_plans[reviewed],
                                          reviewer, context.role_plans[reviewer])
        required = spec.get("independent", True)
        reviews.append({"reviewed": reviewed, "reviewer": reviewer,
                        "policy": "required" if required else "waived",
                        "decision": "not_independent" if reason else "independent", "reason": reason,
                        "label": "review not independent" if not required else "review needs setup" if reason else "independent review configured",
                        "scope": INDEPENDENCE_LIMIT})
        if required and reason:
            blockers.append("flow.review_not_independent")
            diagnostics.append(_diagnostic("flow.review_not_independent", reason,
                                           "Connect a second harness, use Build and check, or author an explicit flow override.", level="error"))
    label = "review not independent" if any(r["policy"] == "waived" for r in reviews) else (
        "review needs setup" if any(r["decision"] == "not_independent" for r in reviews) else
        "independent review configured" if reviews else "no review")
    trusted_checks = checks_with_authority(context.resolved)
    if context.kind == "flow":
        verification = "unavailable"
        if starter_id == "build-review":
            diagnostics.append(_diagnostic("verification.flow_unavailable", "This flow has no acceptance-check phase in this release.",
                                           "Use Build and check for an acceptance receipt from the existing role run."))
    else:
        verification = "from-acceptance-receipt"
    if starter_id in {"build-review", "run-with-role"} and not trusted_checks:
        diagnostics.append(_diagnostic("verification.no_trusted_check", "No trusted or explicitly requested checks are configured.",
                                       "Run `garuda init --project` to propose checks, or `garuda config trust` for a changed project file."))
    if starter_id == "ask-role":
        diagnostics.append(_diagnostic("starter.live_checkout", "This no-edits question uses the live checkout. Another writer can cause output withholding.",
                                       "Prefer an idle checkout for a stable answer."))
    if context.resolved.withheld:
        diagnostics.append(_diagnostic("config.project_untrusted", "Project settings are withheld until their exact bytes are trusted.",
                                       "Review `garuda config trust`."))
    remedies = (_independence_remedies(context.target)
                if blockers and set(blockers) == {"flow.review_not_independent"} and not unchecked else [])
    return {"status": "needs-setup" if blockers else "not-checked" if unchecked else "ready",
            "can_run": not (blockers or unchecked), "review_label": label, "reviews": reviews,
            "bindings": {name: plan.record() for name, plan in context.role_plans.items()},
            "remedies": remedies, "diagnostics": diagnostics, "runtime_evidence": evidence,
            "verification": verification, "check_count": len(trusted_checks)}


class StarterService:
    """List and preview without subprocesses, discovery, admission or store writes."""

    def __init__(self, store: SessionStore | None = None):
        self.store = store or SessionStore()

    def validate_record(self, record: dict, task: str, workspace: str | Path) -> None:
        """Revalidate a frozen starter request immediately before queue dispatch."""
        if not isinstance(record, dict) or not isinstance(record.get("inputs"), dict):
            raise StarterError("starter.plan_invalid", "queued starter metadata is unreadable")
        current, context = compile_with_context(
            record.get("starter_id"), record["inputs"], workspace, store=self.store,
            allow_cross_project_context=record.get("context_grant") == "user-request")
        if current.launch_metadata() != record or current.task != task:
            raise StarterError("starter.preview_changed", "queued starter inputs, sources or configuration changed")
        if not readiness(context, starter_id=current.starter_id)["can_run"]:
            raise StarterError("starter.not_ready", "queued starter readiness changed")

    async def start(self, plan: LaunchPlan) -> dict:
        """Recompile before dispatch; a cached task never authorizes a launch."""
        from garuda.flows.service import FlowExecutionService

        if not isinstance(plan, LaunchPlan):
            raise StarterError("starter.plan_invalid", "compile a starter before starting it")
        current, context = compile_with_context(
            plan.starter_id, plan.inputs, plan.workspace, store=self.store,
            allow_cross_project_context=plan.provenance.get("context_grant") == "user-request")
        if current.digest != plan.digest:
            raise StarterError("starter.preview_changed", "starter inputs, sources or configuration changed; preview again")
        state = readiness(context, starter_id=plan.starter_id)
        if not state["can_run"]:
            raise StarterError("starter.not_ready", "resolve the starter's readiness diagnostics before starting")
        if current.kind == "flow":
            result = await FlowExecutionService(self.store).run(
                current.target, current.task, current.workspace,
                starter_record=current.launch_metadata())
            return {"session_id": result.flow.flow_session,
                    "exit_code": 0 if result.flow.completed else 3}
        from garuda.agents.requests import execute_role_request

        return await execute_role_request(current, self.store)

    def result(self, session_id: str, *, workspace: str | Path | None = None, limit: int = 50, offset: int = 0) -> dict:
        from garuda.core.read_model import starter_result

        if workspace is not None:
            import re

            from garuda.context.tags import resolve
            from garuda.core.session_records import ReadOnlySessions

            if not re.fullmatch(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}', session_id):
                (tag,) = resolve(ReadOnlySessions(self.store.root), workspace_path(workspace),
                                 with_refs=[session_id], read_only=True)
                session_id = tag.session_id
        return starter_result(self.store, session_id, limit=limit, offset=offset)

    def list(self, workspace: str | Path) -> list[dict]:
        root = workspace_path(workspace)
        rows = []
        for entry in load_catalog().values():
            try:
                context = resolve_launch(entry, {}, root)
                state = readiness(context, starter_id=entry.id)
            except RESOLUTION_ERRORS as exc:
                state = _refusal(exc)
            rows.append({"id": entry.id, "title": entry.title, "launch": entry.launch,
                         "fields": entry.fields, "readiness": state})
        return rows

    def preview(self, entry_id: str, inputs: dict, workspace: str | Path, *,
                allow_cross_project_context: bool = False) -> dict:
        plan, context = compile_with_context(entry_id, inputs, workspace, store=self.store,
                                             allow_cross_project_context=allow_cross_project_context)
        return {"plan": plan.to_dict(), "readiness": readiness(context, starter_id=entry_id),
                "configuration": setup_view.configuration(context.resolved)}

    def build_and_check(self, inputs: dict, workspace: str | Path, *, checks: list[str] | None = None,
                        allow_cross_project_context: bool = False) -> dict:
        """Compile an explicitly selected remedy; never launch or add a waiver."""
        original = validate_inputs(load_catalog()["build-review"], inputs)
        if original.get("variant") == "pair":
            raise StarterError("starter.plan_required", "Build and check cannot discard a selected plan handoff; validated handoff is follow-on work")
        selected = {key: value for key, value in original.items() if key != "variant"}
        selected["role"] = "coder"
        if checks is not None:
            selected["options"] = {"checks": checks}
        return self.preview("run-with-role", selected, workspace,
                            allow_cross_project_context=allow_cross_project_context)
