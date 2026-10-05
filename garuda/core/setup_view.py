"""The Setup view's read model (plan task F.4, #169): what is configured and what is wrong.

Read-only, and it starts nothing: no vendor CLI is run (a harness's login state is the last
recorded conclusion, or "not checked"), no model is called. It reports

* **diagnostics** — configuration, harness and workspace-state findings, each with its code
  and a *fix* the person can copy;
* **provenance** — which layer (package, user, trusted project, CLI) supplied every effective
  value, and what a project file asked for that is withheld until trusted;
* **roles** — harness, exact model id, effort and fallback chain;
* **agents** — every definition with its source, declared settings, digest, static prompt
  digest and per-section sizes (no instruction or prompt text);
* **consults** — role grants, the ceilings, and per-adapter transport status;
* **flows** — every flow, the packaged examples marked as such, and any role a flow needs
  that is not defined.
"""

from __future__ import annotations

from typing import Any

from garuda.diagnostics import Diagnostic, diagnostic


def _harness_diagnostics(workspace: str, resolved) -> list[Diagnostic]:
    from garuda.acp.login_probe import LoginState, cached_login
    from garuda.agents.setup import prepare_runtime_catalog

    catalog = prepare_runtime_catalog(workspace)
    wanted = set()
    for spec in (resolved.config.get("roles", {}) if resolved else {}).values():
        wanted.add(spec.get("harness"))
        wanted.update(c.get("harness") for c in spec.get("fallback", []))
    out = []
    for manifest in catalog.registry.manifests:
        rid = manifest.runtime_id
        if manifest.kind.value == "native":
            continue
        if rid not in wanted:
            out.append(diagnostic("harness.not_checked", f"{rid}: not used by your roles",
                                  runtime=rid))
            continue
        if rid in catalog.registry.disabled_ids:
            out.append(diagnostic("harness.disabled", f"{rid}: disabled", level="warning",
                                  runtime=rid))
            continue
        record = next((r for r in catalog.discover(only=frozenset({rid}), cache_ttl=60.0)
                       if r.runtime_id == rid), None)
        if record is None or not record.available:
            out.append(diagnostic("harness.cli_missing", f"{rid}: executable not found",
                                  level="error", setup=manifest.setup or "see its docs"))
            continue
        cached = cached_login(manifest)
        head = f"{rid} {record.version}"
        if cached is None:
            out.append(diagnostic("harness.not_checked", f"{head}: login not checked yet",
                                  runtime=rid))
        elif cached[0] is LoginState.LOGGED_OUT:
            out.append(diagnostic("harness.logged_out", f"{head}; logged out", level="error",
                                  login=manifest.login.instructions or "see its docs"))
        elif cached[0] is LoginState.AUTHENTICATED:
            out.append(diagnostic("harness.ok", f"{head}; logged in (last check)"))
        else:
            out.append(diagnostic("harness.login_unknown", f"{head}; login state {cached[0].value}"))
    return out


def _roles(resolved) -> list[dict]:
    roles = (resolved.config.get("roles", {}) if resolved else {})
    rows = []
    for name, spec in sorted(roles.items()):
        rows.append({
            "role": name, "harness": spec.get("harness"), "model_id": spec.get("model_id"),
            "effort": spec.get("effort"), "permissions": spec.get("permissions"),
            "fallback": [{"harness": c.get("harness"), "model_id": c.get("model_id")}
                         for c in spec.get("fallback", [])],
            "source": resolved.provenance.get(f"roles.{name}"),
        })
    return rows


def _consults(resolved) -> dict:
    """Who may ask whom, the ceilings, and which ACP adapters could host the tool."""
    from garuda.consult import transports
    from garuda.consult.limits import from_config

    roles = (resolved.config.get("roles", {}) if resolved else {})
    grants = [{"asker": name, "targets": list(spec.get("consult") or []),
               "asker_harness": spec.get("harness"),
               # an external asker is offered the tool only on a proved adapter (G.3)
               "transport": ("native" if spec.get("harness") == "native" else "acp")}
              for name, spec in sorted(roles.items()) if spec.get("consult")]
    limits = from_config(resolved.config).to_dict() if resolved else {}
    adapters = []
    for gate in transports.GATES:
        decision = transports.exposure(gate.package, gate.version)
        adapters.append({"package": gate.package, "version": gate.version, **gate.outcomes(),
                         "exposed": decision.exposed, "reason": decision.reason})
    return {"grants": grants, "limits": limits, "adapters": adapters,
            "note": "Native askers can always consult. An ACP asker is offered the tool only "
                    "for an adapter whose three transport gates are all proved."}


def _agents(workspace: str) -> list[dict]:
    from garuda.agents import inspect

    try:
        return inspect.dashboard_rows(workspace)
    except Exception as exc:  # the rest of Setup still loads
        return [{"name": "(agents)", "source": "unknown",
                 **inspect.dashboard_problem(code=getattr(exc, "code", None))}]


def _flows(resolved) -> list[dict]:
    from garuda.config import garuda_yaml as gy
    from garuda.flows import packaged

    rows = []
    for name, (flow, source) in sorted(packaged.available(resolved).items()):
        rows.append({"name": name, "source": source, "example": source == gy.PACKAGE,
                     "steps": len(flow.get("steps", [])),
                     "roles": packaged.required_roles(flow),
                     "missing_roles": packaged.missing_roles(flow, resolved)})
    return rows


def setup(workspace: str) -> dict[str, Any]:
    from garuda.config import garuda_yaml as gy
    from garuda.interfaces.onboarding import _config_diagnostics, _state_diagnostics

    diagnostics, resolved = _config_diagnostics(workspace)
    diagnostics += _harness_diagnostics(workspace, resolved)
    diagnostics += _state_diagnostics()
    return {
        "diagnostics": [d.to_dict() for d in diagnostics],
        "roles": _roles(resolved),
        "flows": _flows(resolved),
        "consults": _consults(resolved),
        "agents": _agents(workspace),
        "provenance": ([{"key": k, "source": v} for k, v in sorted(resolved.provenance.items())]
                       if resolved else []),
        "withheld": list(resolved.withheld) if resolved else [],
        "files": {"user": str(gy.user_path()), "project": str(gy.project_path(workspace))},
    }
