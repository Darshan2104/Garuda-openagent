"""Exact role resolution (plan task C.3, #158).

A ``garuda.yaml`` role resolves to one concrete plan: the runtime id, the
**exact** model id, the effort, the permission ceiling, the agent profile,
a digest of the configuration that produced it, and where each came from.
There is no friendly matching here: a model id is used as written (friendly
names are resolved once, in interactive setup, and saved as the exact id).

- **Native** roles set the run's model, reasoning effort, permission mode and
  profile, exactly as the matching flags would.
- **ACP** roles set the agent's own session options with
  ``session/set_config_option`` before the first prompt — but only for an
  adapter identity (runtime id and discovered version) on which the A.3
  capture (#152) exercised those options (:data:`PROVEN_OPTIONS`). The agent
  must still offer the exact id in its ``session/new`` options; a missing or
  changed id, an unproven adapter, or an unproven effort refuses before the
  prompt.

The chosen ids and the adapter identity are recorded on the session.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

#: (runtime id, adapter version) -> {role field: ACP config option id}, from
#: the A.3 captures in tests/fixtures/acp/ where `session/set_config_option`
#: was exercised and accepted.
PROVEN_OPTIONS: dict[tuple[str, str], dict[str, str]] = {
    ("claude", "0.85.0"): {"model_id": "model", "effort": "effort"},
    ("codex", "2.1.1"): {"model_id": "model", "effort": "reasoning_effort"},
}


class RoleRefused(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code


@dataclass(frozen=True)
class RolePlan:
    role: str
    runtime_id: str
    kind: str  # native | acp
    model_id: str | None = None
    effort: str | None = None
    permissions: str | None = None
    write_policy: str = "edits"
    profile: str | None = None
    digest: str = ""
    provenance: dict = field(default_factory=dict)
    #: The user's harness settings (allowed models, confinement image).
    harness: dict = field(default_factory=dict)
    #: The start-time fallback decision (C.9), when the role has a chain.
    fallback: dict | None = None

    def record(self, *, adapter: dict | None = None, options: dict | None = None) -> dict:
        out = {"name": self.role, "runtime_id": self.runtime_id, "kind": self.kind,
               "model_id": self.model_id, "effort": self.effort,
               "permissions": self.permissions, "write_policy": self.write_policy,
               "profile": self.profile, "digest": self.digest, "provenance": self.provenance}
        if self.fallback is not None:
            out["fallback"] = self.fallback
        if adapter is not None:
            out["adapter"] = adapter
        if options is not None:
            out["acp_options"] = options
        return out


def plan_role(resolved, catalog) -> RolePlan | None:
    """The selected role of ``resolved`` as a plan; ``None`` when none is selected.

    The harness must be a runtime the trusted registry knows (an unknown or
    disabled one refuses through the registry's own error).
    """
    spec = resolved.selected_role()
    if spec is None:
        return None
    manifest = catalog.registry.get(spec["harness"])
    kind = getattr(getattr(manifest, "kind", None), "value", None) or str(
        getattr(manifest, "kind", "native"))
    allowed = resolved.config.get("harnesses", {}).get(spec["harness"], {}).get("allowed_models")
    if allowed is not None and spec.get("model_id") and spec["model_id"] not in allowed:
        raise RoleRefused("role.model_not_allowed",
                          f"{spec['model_id']} is not in harnesses.{spec['harness']}.allowed_models")
    digest = hashlib.sha256(json.dumps(
        {"role": resolved.role, "spec": spec,
         "harness": resolved.config.get("harnesses", {}).get(spec["harness"], {})},
        sort_keys=True).encode()).hexdigest()
    keys = [k for k in resolved.provenance if k == f"roles.{resolved.role}"
            or k == "defaults.role" or k == f"harnesses.{spec['harness']}"]
    return RolePlan(
        role=resolved.role, runtime_id=manifest.runtime_id, kind=kind,
        model_id=spec.get("model_id"), effort=spec.get("effort"),
        permissions=spec.get("permissions"), write_policy=spec.get("write_policy", "edits"),
        profile=spec.get("profile"), digest=digest,
        provenance={k: resolved.provenance[k] for k in keys},
        harness=resolved.config.get("harnesses", {}).get(spec["harness"], {}),
    )


def acp_options(plan: RolePlan, adapter_version: str) -> dict[str, str]:
    """The ``session/set_config_option`` settings for an ACP role, or a refusal."""
    wanted = {k: v for k, v in (("model_id", plan.model_id), ("effort", plan.effort)) if v}
    if not wanted:
        return {}
    proven = PROVEN_OPTIONS.get((plan.runtime_id, adapter_version))
    if proven is None:
        raise RoleRefused(
            "role.options_unproven",
            f"setting {', '.join(wanted)} on {plan.runtime_id} {adapter_version} is not proven "
            "for this adapter version; remove them from the role or use a proven adapter",
        )
    missing = [k for k in wanted if k not in proven]
    if missing:
        raise RoleRefused("role.options_unproven",
                          f"{', '.join(missing)} is not proven for {plan.runtime_id}")
    return {proven[k]: v for k, v in wanted.items()}


def check_offered(options: dict[str, str], offered: list) -> None:
    """Every requested value must be one the agent offered in ``session/new``."""
    by_id = {o.get("id"): o for o in offered or [] if isinstance(o, dict)}
    for config_id, value in options.items():
        option = by_id.get(config_id)
        if option is None:
            raise RoleRefused("role.option_missing",
                              f"the agent offers no {config_id!r} option this session")
        values = [v.get("value") for v in option.get("options") or [] if isinstance(v, dict)]
        if value not in values:
            raise RoleRefused(
                "role.model_unavailable" if config_id == "model" else "role.option_unavailable",
                f"{value!r} is not among the agent's {config_id} values "
                f"({', '.join(map(str, values)) or 'none'})",
            )
