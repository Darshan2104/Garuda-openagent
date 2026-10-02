"""Start-time role fallbacks (plan task C.9, #158).

A role's ``fallback`` chain (user file only; a project may only remove
entries) is walked **once, before start**. The role's own harness comes
first; a candidate is skipped only for a recorded reason:

- ``harness.cli_missing`` — its executable is not installed;
- ``harness.logged_out`` — its documented login check, run fresh, says so;
- ``harness.limit_reached`` — a refresh made just now shows a proved,
  same-account exhaustion with a fresh reading and a known future reset
  (E.2's ``LimitStore.assess``); never inferred, and any doubt keeps the
  candidate.

Anything uncertain — an unknown, failed or timed-out login check, an unknown
limit — is **not** a reason: the candidate is taken. Trust, configuration
and confinement errors refuse; they never fall back. Nothing falls back
after a prompt has been sent: the decision is recorded with the role on the
session (and a flow step's receipt) before the runtime starts. If every
candidate is skipped, the run refuses with each reason.
"""

from __future__ import annotations

from dataclasses import replace

from garuda.runtime.roles import RolePlan, RoleRefused


def default_limit_check(catalog, runtime_id: str) -> bool:
    """Whether ``runtime_id`` is exhausted *for fallback purposes*: only for a harness
    whose limit source is proved, by a refresh made now that no prompt is part of."""
    from garuda.observability.limits import PROVED_SOURCES, LimitStore

    if runtime_id not in PROVED_SOURCES:
        return False
    manifest = next((m for m in catalog.registry.manifests if m.runtime_id == runtime_id), None)
    if manifest is None or manifest.auth_probe is None or not manifest.auth_probe.argv:
        return False
    from garuda.acp.limit_probe import refresh_codex

    store = LimitStore()
    executable = manifest.auth_probe.argv[0]
    try:
        return store.assess(runtime_id, lambda: refresh_codex(executable, store)).eligible_exhausted
    except Exception:
        return False  # anything uncertain is not a reason


def _skip_reason(catalog, runtime_id: str, *, login_run=None, limit_check=None) -> str | None:
    from garuda.acp.login_probe import LoginState, probe_login

    resolved = catalog.registry.get(runtime_id)
    if resolved.kind.value == "native":
        return None
    records = [r for r in catalog.discover(only=frozenset({resolved.runtime_id}))
               if r.runtime_id == resolved.runtime_id]
    if not records or not records[0].available:
        return "harness.cli_missing"
    manifest = next(m for m in catalog.registry.manifests
                    if m.runtime_id == resolved.runtime_id)
    if probe_login(manifest, cache_ttl=0, run=login_run) is LoginState.LOGGED_OUT:
        return "harness.logged_out"
    if (limit_check or (lambda rid: default_limit_check(catalog, rid)))(resolved.runtime_id):
        return "harness.limit_reached"
    return None  # unknown, failed or timed out is never a reason


def choose(plan: RolePlan, resolved, catalog, *, login_run=None, limit_check=None) -> RolePlan:
    """The plan to start, with the fallback decision recorded on it."""
    spec = resolved.config.get("roles", {}).get(plan.role, {})
    chain = spec.get("fallback", [])
    if not chain:
        return plan
    harnesses = resolved.config.get("harnesses", {})
    candidates = [plan] + [
        replace(plan, runtime_id=catalog.registry.get(c["harness"]).runtime_id,
                kind=catalog.registry.get(c["harness"]).kind.value,
                model_id=c.get("model_id"), harness=harnesses.get(c["harness"], {}))
        for c in chain
    ]
    skipped = []
    for index, candidate in enumerate(candidates):
        reason = _skip_reason(catalog, candidate.runtime_id, login_run=login_run,
                              limit_check=limit_check)
        if reason is None:
            decision = {
                "primary": {"harness": plan.runtime_id, "model_id": plan.model_id},
                "taken": {"harness": candidate.runtime_id, "model_id": candidate.model_id,
                          "index": index},
                "skipped": skipped,
            }
            return replace(candidate, fallback=decision)
        skipped.append({"harness": candidate.runtime_id, "model_id": candidate.model_id,
                        "reason": reason})
    raise RoleRefused(
        "harness.no_candidate",
        f"no harness for role {plan.role} can start: "
        + "; ".join(f"{s['harness']} ({s['reason']})" for s in skipped),
    )
