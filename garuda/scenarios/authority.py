"""Server-held authority for dashboard flow starters.

The browser receives this envelope with a preview and returns only its digest.
The envelope is descriptive, not a credential: the protected start request and
the server's current policy remain the authority to execute.
"""

from __future__ import annotations

from garuda.runtime.roles import acp_permission
from garuda.scenarios.digests import digest

_PERMISSION_RANK = {"readonly": 0, "smart": 1, "auto": 2, "yolo": 3}


def _effective_permission(context, role: str, *, no_edits: bool) -> str:
    plan = context.role_plans[role]
    if plan.kind == "acp":
        return acp_permission(plan, no_edits=no_edits)
    if no_edits:
        return "readonly"
    configured = context.resolved.config.get("roles", {}).get(role, {})
    mode = plan.permissions or configured.get("permissions")
    if mode is None and plan.agent_spec is not None:
        try:
            mode = plan.agent_spec.resolved().leaves.get("permissions.mode")
        except (AttributeError, TypeError, ValueError):
            mode = None
    return mode if mode in _PERMISSION_RANK else "smart"


def _role_scope(context, role: str, *, no_edits: bool, max_permission: str, block) -> dict:
    """One role's admitted identity, after the blockers shared by every launch kind."""
    role_plan = context.role_plans[role]
    configured = context.resolved.config.get("roles", {}).get(role, {})
    candidates = context.fallback_plans.get(role, (role_plan,))
    if configured.get("fallback") and len(candidates) != len(configured["fallback"]) + 1:
        block("starter.fallback_unprepared", f"Role {role} has an unresolved fallback chain.",
              "Preview the starter again.")
    for candidate in candidates[1:]:
        permission = (acp_permission(candidate, no_edits=no_edits) if candidate.kind == "acp"
                      else "readonly" if no_edits else candidate.permissions)
        if _PERMISSION_RANK.get(permission, 99) > _PERMISSION_RANK.get(max_permission, -1):
            block("starter.permission_exceeds_ceiling",
                  f"Fallback {candidate.runtime_id} for {role} exceeds the dashboard ceiling.",
                  "Tighten the role or its agent profile.")
        if candidate.kind == "acp" and permission == "readonly":
            import shutil

            from garuda.workspace.confined_acp import Confinement

            if Confinement.from_config(candidate.harness) is None or not shutil.which("docker"):
                block("workspace.readonly_unenforced", f"Fallback {candidate.runtime_id} requires confinement.",
                      "Configure its user confinement image and install Docker.")
    permission = _effective_permission(context, role, no_edits=no_edits)
    if (_PERMISSION_RANK.get(permission, 99)
            > _PERMISSION_RANK.get(max_permission, -1)):
        block("starter.permission_exceeds_ceiling",
              f"Role {role} requests {permission}, above the dashboard ceiling {max_permission}.",
              "Tighten the role or explicitly restart the dashboard with that ceiling.")
    if role_plan.kind == "acp" and permission == "readonly":
        import shutil

        from garuda.workspace.confined_acp import Confinement

        confinement = Confinement.from_config(role_plan.harness)
        if confinement is None or not shutil.which("docker"):
            block("workspace.readonly_unenforced",
                  f"Role {role} requires a configured confinement image and Docker executable.",
                  "Configure the user harness confinement image and install Docker; launch will prove confinement.")
    return {"runtime_id": role_plan.runtime_id, "kind": role_plan.kind,
            "model_id": role_plan.model_id, "role_digest": role_plan.digest,
            "permission": permission, "write_policy": role_plan.write_policy,
            **({"candidates": [candidate.record() for candidate in candidates]} if len(candidates) > 1 else {})}


def _host_blockers(max_permission: str, backend_available: bool,
                   approval_available: bool, block) -> None:
    if max_permission not in _PERMISSION_RANK:
        block("starter.permission_ceiling_invalid",
              "The dashboard permission ceiling is unsupported.",
              "Restart the dashboard with a supported permission ceiling.")
    if not backend_available:
        block("starter.ownership_unavailable",
              "Owned starter runs require the Linux descendant supervisor.",
              "Run this starter from a Garuda dashboard on Linux.")
    if not approval_available:
        block("starter.approval_unavailable",
              "The dashboard approval broker is unavailable.",
              "Restart the dashboard in write mode with its agent loop attached.")


def _capabilities(backend_available: bool, approval_available: bool) -> dict:
    return {
        "process_cleanup": "linux-subreaper" if backend_available else "unavailable",
        "approvals": "dashboard-broker" if approval_available else "unavailable",
        "strict_parent_publication": True,
        "automatic_relaunch": False,
    }


def _checked(plan) -> bool:
    """Whether this flow start runs checks at finalization (a checked owned flow)."""
    return any(check.get("execution") == "available-at-flow-finalization"
               for check in getattr(plan, "checks", None) or ())


def _owned_shape_blockers(name: str, flow: dict, block, *, checked: bool) -> None:
    """Flow shapes the owned engine refuses after admission, refused before it.

    Step ``retries`` follow the CLI rule (#810): a reviewed step, the terminal
    reviewer of a reviewed flow and a parallel group refuse them when the
    configuration loads (for every caller), and a checked flow refuses them with
    the engine's own ``flow.retries_unsupported``; a dashboard-owned flow without
    checks runs them. A non-shared ``isolation`` refuses
    (``starter.isolation_unsupported``); parallel groups are refused below.
    Packaged flows have none of these shapes, so the packaged starters'
    authority is unchanged. Blockers are not part of the envelope or its digest.
    """
    from garuda.config.garuda_yaml import effective_flow_steps
    from garuda.flows.engine import RETRIES_UNSUPPORTED, retries_refused

    for step in effective_flow_steps(flow):
        if step.get("retries") and retries_refused(verification=checked):
            block(RETRIES_UNSUPPORTED,
                  f"Step {step['id']} sets retries: {step['retries']}, which a checked flow "
                  "never runs: its evidence and resume checkpoint assume a stopped attempt "
                  "ends the run.",
                  f"Remove the step's retries or the checks; `garuda flow run {name}` without "
                  "--check runs them.")
    isolation = flow.get("isolation", "shared")
    if isolation != "shared":
        block("starter.isolation_unsupported",
              f"Flow {name} sets isolation: {isolation}; a dashboard start runs only in the "
              "shared checkout.",
              f"Run `garuda flow run {name}` from a terminal, or remove the flow's isolation.")


def start_authority(plan, context, **policy) -> dict:
    """The authority envelope for the plan's launch kind."""
    if plan.kind == "run" and context.kind == "run":
        return run_authority(plan, context, **policy)
    return flow_authority(plan, context, **policy)


def run_authority(plan, context, *, max_permission: str, server_epoch: str,
                  backend_available: bool, approval_available: bool) -> dict:
    """Server policy for one owned run-kind start (run-with-role, ask-role).

    Only options the owned role-run parent actually honours are admitted;
    every other run option refuses here instead of being silently dropped.
    """
    blockers: list[dict[str, str]] = []

    def block(code: str, message: str, fix: str) -> None:
        blockers.append({"code": code, "message": message, "fix": fix,
                         "level": "error"})

    if plan.kind != "run" or context.kind != "run" or context.flow is not None:
        block("starter.launch_kind_mismatch",
              "This preview is not a run-kind starter.",
              "Preview the starter again.")
    _host_blockers(max_permission, backend_available, approval_available, block)
    options = dict(plan.options)
    configured = context.resolved.config.get("roles", {}).get(plan.target, {})
    # The engine applies the starter's no-edits and the role's own policy alike.
    no_edits = (bool(plan.provenance.get("no_edits"))
                or configured.get("write_policy") == "no-edits")
    role = None
    if plan.target in context.role_plans:
        role = _role_scope(context, plan.target, no_edits=no_edits,
                           max_permission=max_permission, block=block)
    else:
        block("starter.role_unprepared", f"Role {plan.target} was not resolved.",
              "Preview the starter again.")
    envelope = {
        "version": 1,
        "kind": "run",
        "server_epoch": server_epoch,
        "workspace": {"path": plan.workspace,
                      "identity": plan.provenance.get("workspace_identity")},
        "operator": {"max_permission": max_permission},
        "run": {"target": plan.target, "role": role, "no_edits": no_edits,
                "workspace_mode": options.get("isolation", "shared"), "options": options,
                "lease_mode": "read-only" if no_edits and not plan.checks else "mutating",
                "checks": [{"definition_digest": check["definition_digest"],
                            "authority": check["authority"]} for check in plan.checks]},
        "capabilities": {**_capabilities(backend_available, approval_available),
                         "checks": "owned-acceptance" if plan.checks else "none",
                         **({"approvals": "durable-session-channel", "queue": "owned-worker"}
                            if options.get("bg") else {})},
    }
    return {"digest": digest(envelope), "can_start": not blockers,
            "envelope": envelope, "diagnostics": blockers}


def flow_authority(plan, context, *, max_permission: str, server_epoch: str,
                   backend_available: bool, approval_available: bool) -> dict:
    """Build the exact server policy a later Start request must match."""
    blockers: list[dict[str, str]] = []

    def block(code: str, message: str, fix: str) -> None:
        blockers.append({"code": code, "message": message, "fix": fix,
                         "level": "error"})

    if plan.kind != "flow" or context.kind != "flow" or context.flow is None:
        block("starter.launch_kind_mismatch",
              "This preview is not a flow starter.",
              "Preview the starter again.")
    _host_blockers(max_permission, backend_available, approval_available, block)

    roles = {}
    steps = []
    flow = context.flow or {"steps": []}
    _owned_shape_blockers(plan.target, flow, block, checked=_checked(plan))
    for index, step in enumerate(flow.get("steps", [])):
        if step.get("parallel"):
            from garuda.flows.engine import PARALLEL_OWNED_REASON

            block("starter.parallel_ownership_unavailable",
                  f"Step {step.get('id')} is a parallel group, which a dashboard start never "
                  f"runs: {PARALLEL_OWNED_REASON}.",
                  f"Run `garuda flow run {plan.target}` from a terminal, which runs parallel "
                  "groups, or make the step sequential for one-click Start.")
        names = [step.get("role"), *step.get("parallel", [])]
        reviewer = step.get("review", {}).get("by")
        if reviewer:
            names.append(reviewer)
        for role in names:
            if not role or role not in context.role_plans:
                continue
            configured = context.resolved.config.get("roles", {}).get(role, {})
            no_edits = (step.get("write_policy") or configured.get("write_policy")) == "no-edits"
            roles[role] = _role_scope(context, role, no_edits=no_edits,
                                      max_permission=max_permission, block=block)
            steps.append({"index": index, "id": step.get("id"), "role": role,
                          "permission": roles[role]["permission"], "no_edits": no_edits})

    envelope = {
        "version": 1,
        "server_epoch": server_epoch,
        "workspace": {"path": plan.workspace,
                      "identity": plan.provenance.get("workspace_identity")},
        "operator": {"max_permission": max_permission},
        "flow": {"target": plan.target,
                 "source": plan.provenance.get("flow_source"),
                 "roles": {name: roles[name] for name in sorted(roles)},
                 "steps": steps},
        "capabilities": _capabilities(backend_available, approval_available),
    }
    return {"digest": digest(envelope), "can_start": not blockers,
            "envelope": envelope, "diagnostics": blockers}
