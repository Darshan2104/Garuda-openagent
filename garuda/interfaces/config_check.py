"""`garuda config check`: each role's options, checked before a run (#714).

For every role in the effective ``garuda.yaml``, and every entry of its
``fallback`` chain, the checks a launch makes before its first prompt are run
here instead, so a problem is reported before any run:

- without ``--probe`` nothing is executed: the harness must be known, the
  model allowed, the harness mode within the role's permissions, and the
  model, effort and mode settable on some proven adapter version;
- with ``--probe`` each ACP harness is launched once per role entry, without
  a prompt (:mod:`garuda.acp.options_probe`). Its reported version must be
  proven for the options, and the model, effort and the mode the role's
  permissions select must be offered for that model and taken. A model that
  is not offered only warns when a later ``fallback`` entry exists, because a
  run then moves on to it.

A read-only ACP role runs in its Docker confinement image, whose adapter is not
the host one, so it is not probed.
"""

from __future__ import annotations

from dataclasses import replace

OK, WARNING, ERROR, NOT_CHECKED = "ok", "warning", "error", "not-checked"


def _row(role: str, entry: str, plan, status: str, detail: str, code: str | None = None) -> dict:
    return {"role": role, "entry": entry,
            "harness": getattr(plan, "runtime_id", None), "model_id": getattr(plan, "model_id", None),
            "status": status, "code": code, "detail": detail}


def _check_entry(role: str, entry: str, plan, catalog, *, probe: bool, probe_plan) -> dict:
    from garuda.runtime.roles import RoleRefused, acp_options, acp_permission, check_provable, harness_mode

    if plan.kind != "acp":
        return _row(role, entry, plan, OK, "native runtime; nothing to probe")
    try:
        harness_mode(plan)
        manifest = catalog.registry.manifest_for(plan.runtime_id)
        if acp_permission(plan) == "readonly":
            check_provable(plan)
            return _row(role, entry, plan, NOT_CHECKED,
                        "read-only: runs in its Docker confinement image, which is not probed")
        if not probe:
            if manifest.version_args:
                check_provable(plan)
            else:
                acp_options(plan, manifest.version)
            return _row(role, entry, plan, NOT_CHECKED,
                        "settable on a proven adapter; run with --probe to check what "
                        "the installed harness offers")
    except RoleRefused as exc:
        return _row(role, entry, plan, ERROR, str(exc), exc.code)
    records = [r for r in catalog.discover(only=frozenset({plan.runtime_id}))
               if r.runtime_id == plan.runtime_id]
    if not records or not records[0].available or not records[0].executable:
        return _row(role, entry, plan, ERROR, f"{plan.runtime_id} is not installed",
                    "harness.cli_missing")
    answer = probe_plan(manifest, records[0], plan)
    if answer is None:
        return _row(role, entry, plan, NOT_CHECKED,
                    f"{plan.runtime_id} did not answer the probe; checked again at launch")
    if answer.refusal is not None:
        return _row(role, entry, plan, ERROR, f"{plan.runtime_id} {answer.version}: "
                    f"{answer.refusal}", answer.refusal.code)
    options = acp_options(plan, answer.version)
    shown = ", ".join(f"{k}={v}" for k, v in sorted(options.items())) or "harness defaults"
    return _row(role, entry, plan, OK, f"{plan.runtime_id} {answer.version} offers {shown}")


def check_roles(workspace, *, probe: bool = False, probe_plan=None) -> list[dict] | None:
    """One row per role entry; ``None`` when there is no ``garuda.yaml``.

    ``probe_plan(manifest, record, plan)`` answers like
    :func:`garuda.acp.options_probe.check_plan` (the default).
    """
    from garuda.acp.catalog import RuntimeSettingsError
    from garuda.agents.fallbacks import candidates
    from garuda.agents.setup import prepare_runtime_catalog
    from garuda.config import garuda_yaml as gy
    from garuda.runtime.registry import RegistryError
    from garuda.runtime.roles import RoleRefused, plan_role

    if probe_plan is None:
        from garuda.acp.options_probe import check_plan as probe_plan
    resolved = gy.load_effective(workspace)
    if resolved is None:
        return None
    catalog = prepare_runtime_catalog(workspace)
    rows = []
    for name in sorted(resolved.config.get("roles", {})):
        role_config = replace(resolved, role=name)
        try:
            plan = plan_role(role_config, catalog)
            chain = candidates(plan, role_config, catalog)
        except (RoleRefused, RegistryError, RuntimeSettingsError) as exc:
            rows.append({"role": name, "entry": "role", "harness": None, "model_id": None,
                         "status": ERROR, "code": getattr(exc, "code", None), "detail": str(exc)})
            continue
        for index, entry in enumerate(chain):
            row = _check_entry(name, "role" if index == 0 else f"fallback {index}", entry,
                               catalog, probe=probe, probe_plan=probe_plan)
            if row["code"] == "role.model_unavailable" and index < len(chain) - 1:
                # A run skips this entry for the next one in the chain (#714).
                row["status"] = WARNING
                row["detail"] += "; a run moves on to the next fallback entry"
            rows.append(row)
    return rows


def render(rows: list[dict]) -> list[str]:
    lines = []
    for row in rows:
        where = row["role"] + ("" if row["entry"] == "role" else f" ({row['entry']})")
        target = " ".join(str(v) for v in (row["harness"], row["model_id"]) if v)
        lines.append(f"[{row['status']}] {where}" + (f": {target}" if target else "")
                     + f" — {row['detail']}")
    return lines


def run(args) -> int:
    """`garuda config check [--probe] [--json]`: 0 when nothing refuses, 1 otherwise."""
    import json
    import sys

    from garuda.config.garuda_yaml import GarudaConfigError

    try:
        rows = check_roles(args.workspace, probe=args.probe)
    except GarudaConfigError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    if rows is None:
        print("No garuda.yaml (user or project). `garuda init` proposes one.")
        return 0
    if args.json:
        print(json.dumps(rows, indent=2))
    else:
        print("\n".join(render(rows)) if rows else "No roles to check.")
    return 1 if any(row["status"] == ERROR for row in rows) else 0
