"""A flow's pipeline as the run-flow preview (and the Flows page) shows it.

Pure read. Nothing here probes a runtime, starts a subprocess, opens a socket
or writes: it projects the role plans that compilation already resolved
(``project``) or, for a flow whose roles cannot all be resolved, the raw
configuration (``project_static``). The engine's own pure helpers decide limits,
``long_run`` and the owned backstop; when one cannot be imported or refuses,
the configured value is shown with ``"basis": "configured"`` instead.

Values that are not known here are ``null`` and listed in ``unknown`` with the
reason; nothing is guessed. Shape (``version`` 1)::

    {"version": 1,
     "flow": NAME, "source": "packaged" | "user" | "project" | <raw label>,
     "description": str | null,
     "resolution": "prepared" (resolved role plans) | "static" (configuration only),
     "isolation": {"configured": "shared" | "worktree" | "auto" | null,
                   "owned": "shared"},           # dashboard starts refuse anything else
     "missing_roles": [ROLE, ...],
     "steps": [STEP, ...],
     "edges": [EDGE, ...],
     "unknown": [{"path": "steps[1].members[0].model_id", "reason": str}, ...]}

    STEP = {"id": str,                       # the engine's effective id (role-N when unset)
            "index": int, "kind": "role" | "parallel",
            "write_policy": "edits" | "no-edits" | null,   # as the step configures it
            "members": [MEMBER, ...],        # one for a role step; each group member
            "inputs": [{"type": ARTIFACT, "from": STEP_ID | "GROUP:ROLE" | null,
                        "producer": STEP_ID | null}],     # producer: static resolution
            "outputs": [ARTIFACT, ...],
            "review": {"by": ROLE, "max_rounds": int, "independent": bool} | null,
            "retries": int,
            "limits": {"deadline_min": n | null, "silence_min": n | null,
                       "tool_silence_min": n | null,
                       "basis": "engine" | "configured"},   # step over flow, key by key
            "long_run": {"max_hours", "session_turns", "session_hours", "max_turns",
                         "no_progress_turns", "source": "step" | "flow",
                         "applies": bool | null} | null,     # only ACP steps run it
            "owned_backstop": {"timeout_min": n | null,
                               "deadline_min": n | null,    # effective, long_run included
                               "rule": "deadline-plus-margin" | "default-without-deadline" | null,
                               "on_expiry": str}}

    MEMBER = {"role": ROLE, "runtime_id": str | null, "kind": "acp" | "native" | null,
              "model_id": str | null, "effort": str | null, "harness_mode": str | null,
              "permission": {"configured": str | null, "effective": str | null},
              "write_policy": {"role": "edits" | "no-edits" | null,
                               "effective": "edits" | "no-edits"},
              "fallbacks": [{"runtime_id", "kind", "model_id", "effort",
                             "permission"}, ...]}     # in the order they would be tried

    EDGE = {"from": STEP_ID, "to": STEP_ID,
            "kind": "input" | "review" | "sequence",
            "type": ARTIFACT | null, "member": ROLE | null}

``permission.effective`` is what the role runs with before the dashboard
ceiling; the authority envelope refuses a start whose role exceeds it.
``owned_backstop`` is the Linux supervisor's kill timeout for one owned step:
its deadline plus a margin, or one hour when the step has no deadline; a step
that outlives it is killed and the flow is quarantined.
"""

from __future__ import annotations

from typing import Any

from garuda.config import garuda_yaml as gy

VERSION = 1
#: ``garuda flow list``'s labels for a flow's provenance (``packaged.SOURCE_LABELS``).
_SOURCES = {gy.PACKAGE: "packaged", gy.USER: "user", gy.PROJECT: "project"}
_EXPIRY = "the supervisor kills the step's worker and the flow is quarantined"
#: Effective step limits that could not be derived (distinct from ``None``: no limits).
_UNKNOWN = object()


def project(context) -> dict:
    """The pipeline of a compiled flow starter's resolved launch context."""
    return _project(context.target, context.flow or {"steps": []}, context.source,
                    context.resolved, context.role_plans, context.fallback_plans,
                    context=context, resolution="prepared")


def project_static(name: str, flow: dict, source: str, resolved) -> dict:
    """The pipeline of a flow from configuration alone (roles may be missing)."""
    return _project(name, flow, source, resolved, {}, {}, context=None, resolution="static")


def _project(name, flow, source, resolved, role_plans, fallback_plans, *, context,
             resolution) -> dict:
    unknown: list[dict] = []
    config = (resolved.config if resolved is not None else {}) or {}
    roles = config.get("roles", {})
    steps = _effective_steps(flow, unknown)
    out_steps = [_step(flow, step, roles, role_plans, fallback_plans, context, unknown,
                       f"steps[{i}]") for i, step in enumerate(steps)]
    required = []
    for step in steps:
        for role in [step.get("role"), *step.get("parallel", []),
                     (step.get("review") or {}).get("by")]:
            if role and role not in required:
                required.append(role)
    return {"version": VERSION, "flow": name, "source": _SOURCES.get(source, source),
            "description": flow.get("description"), "resolution": resolution,
            "isolation": {"configured": flow.get("isolation", "shared"), "owned": "shared"},
            "missing_roles": [role for role in required if role not in roles],
            "steps": out_steps, "edges": _edges(steps), "unknown": unknown}


def _effective_steps(flow: dict, unknown: list) -> list[dict]:
    try:
        from garuda.config.garuda_yaml import effective_flow_steps

        return effective_flow_steps(flow)
    except Exception as exc:  # noqa: BLE001 - an unreadable flow is reported, not guessed
        unknown.append({"path": "steps", "reason": f"step ids could not be derived: {exc}"})
        return [{**step, "id": step.get("id"), "index": i}
                for i, step in enumerate(flow.get("steps", []))]


def _step(flow, step, roles, role_plans, fallback_plans, context, unknown, path) -> dict:
    members = list(step.get("parallel") or [step.get("role")])
    rows = [_member(step, role, roles, role_plans, fallback_plans, context, unknown,
                    f"{path}.members[{j}]") for j, role in enumerate(members)]
    review = step.get("review")
    limits, effective = _limits(flow, step, unknown, path)
    long_run = _long_run(flow, step, rows, unknown, path)
    if long_run is not None and long_run["applies"]:
        effective = _with_long_run(effective, long_run)
    return {"id": step.get("id"), "index": step.get("index"),
            "kind": "parallel" if "parallel" in step else "role",
            "write_policy": step.get("write_policy"),
            "members": rows,
            "inputs": [_input(entry, step, flow) for entry in step.get("inputs", [])],
            "outputs": list(step.get("outputs", [])),
            "review": ({"by": review.get("by"), "max_rounds": review.get("max_rounds", 1),
                        "independent": review.get("independent", True)} if review else None),
            "retries": step.get("retries", 0),
            "limits": limits,
            "long_run": long_run,
            "owned_backstop": _backstop(effective, long_run, unknown, path)}


def _member(step, role, roles, role_plans, fallback_plans, context, unknown, path) -> dict:
    configured = roles.get(role, {}) if role else {}
    no_edits = "no-edits" in (step.get("write_policy"), configured.get("write_policy"))
    plan = role_plans.get(role)
    row: dict[str, Any] = {
        "role": role, "runtime_id": None, "kind": None, "model_id": None, "effort": None,
        "harness_mode": None,
        "permission": {"configured": configured.get("permissions"), "effective": None},
        "write_policy": {"role": configured.get("write_policy"),
                         "effective": "no-edits" if no_edits else "edits"},
        "fallbacks": []}
    if plan is None:
        if role not in roles:
            reason = "the role is not defined in garuda.yaml"
            keys = ("runtime_id", "kind", "model_id", "effort", "harness_mode")
        else:
            # Configured values only; the harness binding is not resolved here.
            reason = "the role's harness binding is not resolved in this view"
            row.update(model_id=configured.get("model_id"), effort=configured.get("effort"),
                       harness_mode=configured.get("harness_mode"))
            keys = ("runtime_id", "kind", "fallbacks")
        for key in keys:
            unknown.append({"path": f"{path}.{key}", "reason": reason})
        unknown.append({"path": f"{path}.permission.effective", "reason": reason})
        return row
    row.update(runtime_id=plan.runtime_id, kind=plan.kind, model_id=plan.model_id,
               effort=plan.effort, harness_mode=plan.harness_mode)
    if row["permission"]["configured"] is None:
        row["permission"]["configured"] = plan.permissions
    for key in ("model_id", "effort"):
        if row[key] is None:
            unknown.append({"path": f"{path}.{key}",
                            "reason": "not set on the role: the harness or default model "
                                      "binding decides it at launch"})
    row["permission"]["effective"] = _permission(context, role, plan, no_edits, unknown,
                                                 f"{path}.permission.effective")
    for k, candidate in enumerate(fallback_plans.get(role, ())[1:]):
        row["fallbacks"].append({
            "runtime_id": candidate.runtime_id, "kind": candidate.kind,
            "model_id": candidate.model_id, "effort": candidate.effort,
            "permission": _candidate_permission(candidate, no_edits, unknown,
                                                f"{path}.fallbacks[{k}].permission")})
    return row


def _permission(context, role, plan, no_edits, unknown, path):
    try:
        from garuda.scenarios.authority import _effective_permission

        return _effective_permission(context, role, no_edits=no_edits)
    except Exception as exc:  # noqa: BLE001 - shown as unknown, never guessed
        unknown.append({"path": path, "reason": f"not derivable here: {exc}"})
        return None


def _candidate_permission(candidate, no_edits, unknown, path):
    try:
        from garuda.runtime.roles import acp_permission

        if candidate.kind == "acp":
            return acp_permission(candidate, no_edits=no_edits)
        return "readonly" if no_edits else candidate.permissions
    except Exception as exc:  # noqa: BLE001
        unknown.append({"path": path, "reason": f"not derivable here: {exc}"})
        return None


def _input(entry, step, flow) -> dict:
    if isinstance(entry, str):
        kind, source = entry, None
    else:
        kind, source = entry.get("type"), entry.get("from")
    return {"type": kind, "from": source, "producer": _producer(kind, source, step, flow)}


def _producer(kind, source, step, flow) -> str | None:
    """The step that supplies an input, resolved statically from the configuration.

    A plain type takes the latest earlier step declaring it (a review loop may
    deliver a later attempt of that same step); ``from`` names its step.
    """
    if source is not None:
        return source.partition(":")[0]
    earlier = [s for s in _ids(flow)[:step.get("index", 0)] if kind in s[1]]
    return earlier[-1][0] if earlier else None


def _ids(flow) -> list[tuple[str, list]]:
    try:
        from garuda.config.garuda_yaml import effective_flow_steps

        return [(s["id"], s.get("outputs", [])) for s in effective_flow_steps(flow)]
    except Exception:  # noqa: BLE001
        return [(s.get("id"), s.get("outputs", [])) for s in flow.get("steps", [])]


def _edges(steps: list[dict]) -> list[dict]:
    edges = []
    flow = {"steps": steps}
    for previous, step in zip(steps, steps[1:], strict=False):
        edges.append({"from": previous["id"], "to": step["id"], "kind": "sequence",
                      "type": None, "member": None})
    for step in steps:
        for entry in step.get("inputs", []):
            row = _input(entry, step, flow)
            if row["producer"] is not None:
                member = (row["from"].partition(":")[2] or None) if row["from"] else None
                edges.append({"from": row["producer"], "to": step["id"], "kind": "input",
                              "type": row["type"], "member": member})
        if step.get("review") and steps:
            edges.append({"from": step["id"], "to": steps[-1]["id"], "kind": "review",
                          "type": "review", "member": None})
    return edges


def _minutes(seconds):
    return None if seconds is None else seconds / 60


def _limits(flow, step, unknown, path) -> tuple[dict, Any]:
    """Configured limits as the engine merges them, and its ``StepLimits``."""
    try:
        from garuda.flows.engine import step_limits

        limits = step_limits(flow, step)
    except Exception as exc:  # noqa: BLE001 - the configured values are still shown
        merged = {**(flow.get("limits") or {}), **(step.get("limits") or {})}
        unknown.append({"path": f"{path}.limits",
                        "reason": f"the engine's limits could not be derived: {exc}"})
        return ({"deadline_min": merged.get("deadline_min"),
                 "silence_min": merged.get("silence_min"),
                 "tool_silence_min": merged.get("tool_silence_min"),
                 "basis": "configured"}, _UNKNOWN)
    if limits is None:
        return ({"deadline_min": None, "silence_min": None, "tool_silence_min": None,
                 "basis": "engine"}, None)
    return ({"deadline_min": _minutes(limits.deadline_sec),
             "silence_min": _minutes(limits.silence_sec),
             "tool_silence_min": _minutes(limits.tool_silence_sec),
             "basis": "engine"}, limits)


def _long_run(flow, step, members, unknown, path) -> dict | None:
    if "long_run" not in step and "long_run" not in flow:
        return None
    try:
        from garuda.flows.long_step import long_run

        settings = long_run(flow, step).record()
    except Exception as exc:  # noqa: BLE001
        settings = {**(flow.get("long_run") or {}), **(step.get("long_run") or {})}
        unknown.append({"path": f"{path}.long_run",
                        "reason": f"only the configured keys are shown: {exc}"})
    # The engine runs a step's turn loop on an ACP role only; a parallel group never.
    kind = members[0]["kind"] if members else None
    applies = False if "parallel" in step else None if kind is None else kind == "acp"
    if applies is None:
        unknown.append({"path": f"{path}.long_run.applies",
                        "reason": "the role's harness is not resolved; only ACP steps run long"})
    return {**settings, "source": "step" if "long_run" in step else "flow", "applies": applies}


def _with_long_run(limits, long_run):
    if limits is _UNKNOWN:
        return _UNKNOWN
    try:
        from garuda.flows.long_step import LongRun, with_deadline

        keys = ("max_hours", "session_turns", "session_hours", "max_turns", "no_progress_turns")
        return with_deadline(limits, LongRun(**{key: long_run[key] for key in keys}))
    except Exception:  # noqa: BLE001
        return _UNKNOWN


def _backstop(limits, long_run, unknown, path) -> dict:
    deadline = None if limits in (None, _UNKNOWN) else limits.deadline_sec
    if limits is _UNKNOWN or (long_run is not None and long_run["applies"] is None):
        unknown.append({"path": f"{path}.owned_backstop",
                        "reason": "the step's effective deadline is not known here"})
        return {"timeout_min": None, "deadline_min": None, "rule": None, "on_expiry": _EXPIRY}
    try:
        from garuda.runtime.owned_role import owned_timeout

        timeout = owned_timeout(limits)
    except Exception as exc:  # noqa: BLE001
        unknown.append({"path": f"{path}.owned_backstop",
                        "reason": f"the supervisor backstop could not be derived: {exc}"})
        return {"timeout_min": None, "deadline_min": _minutes(deadline), "rule": None,
                "on_expiry": _EXPIRY}
    return {"timeout_min": _minutes(timeout), "deadline_min": _minutes(deadline),
            "rule": "deadline-plus-margin" if deadline else "default-without-deadline",
            "on_expiry": _EXPIRY}
