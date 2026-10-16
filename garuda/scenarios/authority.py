"""Server-held authority for dashboard flow starters.

The browser receives this envelope with a preview and returns only its digest.
The envelope is descriptive, not a credential: the protected start request and
the server's current policy remain the authority to execute.
"""

from __future__ import annotations

from garuda.scenarios.digests import digest

_PERMISSION_RANK = {"readonly": 0, "smart": 1, "auto": 2, "yolo": 3}


def _effective_permission(context, role: str, *, no_edits: bool) -> str:
    if no_edits:
        return "readonly"
    plan = context.role_plans[role]
    configured = context.resolved.config.get("roles", {}).get(role, {})
    mode = plan.permissions or configured.get("permissions")
    if mode is None and plan.agent_spec is not None:
        try:
            mode = plan.agent_spec.resolved().leaves.get("permissions.mode")
        except (AttributeError, TypeError, ValueError):
            mode = None
    return mode if mode in _PERMISSION_RANK else "smart"


def flow_authority(plan, context, *, max_permission: str, server_epoch: str,
                   backend_available: bool, approval_available: bool) -> dict:
    """Build the exact server policy a later Start request must match."""
    blockers: list[dict[str, str]] = []

    def block(code: str, message: str, fix: str) -> None:
        blockers.append({"code": code, "message": message, "fix": fix,
                         "level": "error"})

    if plan.kind != "flow" or context.kind != "flow" or context.flow is None:
        block("starter.run_start_unavailable",
              "One-click Start is available only for flow starters.",
              "Copy the equivalent run command until the shared run facade lands.")
    if max_permission not in _PERMISSION_RANK:
        block("starter.permission_ceiling_invalid",
              "The dashboard permission ceiling is unsupported.",
              "Restart the dashboard with a supported permission ceiling.")
    if not backend_available:
        block("starter.ownership_unavailable",
              "Owned flow starts require the Linux descendant supervisor.",
              "Run this starter from a Garuda dashboard on Linux.")
    if not approval_available:
        block("starter.approval_unavailable",
              "The dashboard approval broker is unavailable.",
              "Restart the dashboard in write mode with its agent loop attached.")

    roles = {}
    steps = []
    flow = context.flow or {"steps": []}
    for index, step in enumerate(flow.get("steps", [])):
        if step.get("parallel"):
            block("starter.parallel_ownership_unavailable",
                  "This flow contains a parallel snapshot step without owned closure evidence.",
                  "Use a sequential flow for one-click Start.")
        names = [step.get("role"), *step.get("parallel", [])]
        reviewer = step.get("review", {}).get("by")
        if reviewer:
            names.append(reviewer)
        for role in names:
            if not role or role not in context.role_plans:
                continue
            role_plan = context.role_plans[role]
            configured = context.resolved.config.get("roles", {}).get(role, {})
            if configured.get("fallback"):
                block("starter.fallback_probe_unowned",
                      f"Role {role} has a fallback chain whose launch probe is not owned by this gateway.",
                      "Use a role without fallbacks for one-click Start.")
            no_edits = (step.get("write_policy") or configured.get("write_policy")) == "no-edits"
            permission = _effective_permission(context, role, no_edits=no_edits)
            if (_PERMISSION_RANK.get(permission, 99)
                    > _PERMISSION_RANK.get(max_permission, -1)):
                block("starter.permission_exceeds_ceiling",
                      f"Role {role} requests {permission}, above the dashboard ceiling {max_permission}.",
                      "Tighten the role or explicitly restart the dashboard with that ceiling.")
            if role_plan.kind == "acp" and (no_edits or permission == "readonly"):
                block("starter.acp_readonly_ownership_unavailable",
                      f"Role {role} needs read-only ACP container lifetime ownership that is unavailable.",
                      "Use a supported native role or run the equivalent command manually.")
            roles[role] = {"runtime_id": role_plan.runtime_id, "kind": role_plan.kind,
                           "model_id": role_plan.model_id, "role_digest": role_plan.digest,
                           "permission": permission, "write_policy": role_plan.write_policy}
            steps.append({"index": index, "id": step.get("id"), "role": role,
                          "permission": permission, "no_edits": no_edits})

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
        "capabilities": {
            "process_cleanup": "linux-subreaper" if backend_available else "unavailable",
            "approvals": "dashboard-broker" if approval_available else "unavailable",
            "strict_parent_publication": True,
            "automatic_relaunch": False,
        },
    }
    return {"digest": digest(envelope), "can_start": not blockers,
            "envelope": envelope, "diagnostics": blockers}
