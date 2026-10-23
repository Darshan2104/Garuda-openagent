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
- An ACP role also runs in one of its harness's own permission modes
  (``harness_mode``, :func:`harness_mode`). Each mode needs a minimum Garuda
  permission ceiling, and a no-edits step only a mode that cannot write. A
  role that names none gets the mode its ceiling allows (:data:`DEFAULT_MODES`)
  on a proven adapter, and the adapter's own default elsewhere. Garuda never
  selects a mode that bypasses approval by itself.

The chosen ids and the adapter identity are recorded on the session.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from garuda.agents.spec_api import AgentSpec
    from garuda.runtime.registry import RuntimeRegistry

#: (runtime id, adapter version) -> {role field: ACP config option id}, from
#: the A.3 captures in tests/fixtures/acp/ where `session/set_config_option`
#: was exercised and accepted.
PROVEN_OPTIONS: dict[tuple[str, str], dict[str, str]] = {
    ("claude", "0.85.0"): {"model_id": "model", "effort": "effort", "harness_mode": "mode"},
    ("codex", "2.1.1"): {"model_id": "model", "effort": "reasoning_effort",
                         "harness_mode": "mode"},
}

#: Garuda permission ceilings, least to most authority.
LEVELS = ("readonly", "smart", "auto", "yolo")
#: Each harness mode and the lowest ceiling that may select it, from the
#: adapters' own descriptions (tests/fixtures/acp/). A mode that cannot write
#: needs ``readonly``; one that edits the workspace and asks before risky
#: actions, ``smart`` (Codex ``agent`` asks only for actions it judges unsafe,
#: as smart does); one where the agent decides every permission itself,
#: ``auto``; one that approves everything, ``yolo``.
MODE_AUTHORITY: dict[str, dict[str, str]] = {
    "claude": {"default": "readonly", "plan": "readonly", "acceptEdits": "smart",
               "auto": "auto", "bypassPermissions": "yolo"},
    "codex": {"read-only": "readonly", "workspace-write": "smart", "agent": "smart",
              "agent-full-access": "yolo"},
}
#: The mode a role gets when it names none, by the most its step may do. Never
#: a mode that bypasses approval.
DEFAULT_MODES: dict[str, dict[str, str]] = {
    "claude": {"readonly": "default", "smart": "acceptEdits", "auto": "auto", "yolo": "auto"},
    "codex": {"readonly": "read-only", "smart": "agent", "auto": "agent", "yolo": "agent"},
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
    #: The harness's own permission mode the role asked for; ``None`` for the default.
    harness_mode: str | None = None
    digest: str = ""
    provenance: dict = field(default_factory=dict)
    #: The user's harness settings (allowed models, confinement image).
    harness: dict = field(default_factory=dict)
    #: The start-time fallback decision (C.9), when the role has a chain.
    fallback: dict | None = None
    #: The digest of the resolved agent definition the role runs under (H.10); part of the
    #: execution identity, so changing the definition changes ``digest``.
    agent_digest: str | None = None
    #: An ACP role's agent instructions, sent as a labelled context block with the first
    #: task. Never recorded on the session (only its digest is).
    instructions: str | None = None
    #: Native resolved source held for execution only; never serialized in record().
    agent_spec: AgentSpec | None = field(default=None, repr=False, compare=False)

    #: Start-time trusted alias/disablement evidence, never persisted or compared.
    #: Review preflight uses this same non-executing registry as role resolution.
    identity_registry: RuntimeRegistry | None = field(default=None, repr=False, compare=False)

    def record(self, *, adapter: dict | None = None, options: dict | None = None) -> dict:
        out = {"name": self.role, "runtime_id": self.runtime_id, "kind": self.kind,
               "model_id": self.model_id, "effort": self.effort,
               "permissions": self.permissions, "write_policy": self.write_policy,
               "profile": self.profile, "harness_mode": self.harness_mode,
               "digest": self.digest, "provenance": self.provenance}
        if self.fallback is not None:
            out["fallback"] = self.fallback
        if self.agent_digest is not None:
            out["agent"] = {"name": self.profile, "digest": self.agent_digest}
            if self.instructions:
                out["agent"]["instructions_sha256"] = hashlib.sha256(
                    self.instructions.encode("utf-8")).hexdigest()
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
    plan = RolePlan(
        role=resolved.role, runtime_id=manifest.runtime_id, kind=kind,
        identity_registry=catalog.registry,
        model_id=spec.get("model_id"), effort=spec.get("effort"),
        permissions=spec.get("permissions"), write_policy=spec.get("write_policy", "edits"),
        profile=spec.get("profile"), harness_mode=spec.get("harness_mode"), digest=digest,
        provenance={k: resolved.provenance[k] for k in keys},
        harness=resolved.config.get("harnesses", {}).get(spec["harness"], {}),
    )
    harness_mode(plan)  # a mode the role may not use refuses before anything starts
    return plan


def harness_mode(plan: RolePlan, *, no_edits: bool = False) -> str | None:
    """The harness permission mode to select for ``plan``, or ``None`` to leave
    the adapter's own; refuses a mode the role or step may not use."""
    ceiling = plan.permissions or "smart"
    limit = "readonly" if no_edits or plan.write_policy == "no-edits" else ceiling
    if plan.harness_mode is None:
        return DEFAULT_MODES.get(plan.runtime_id, {}).get(limit)
    needed = MODE_AUTHORITY.get(plan.runtime_id, {}).get(plan.harness_mode)
    if needed is None:
        raise RoleRefused("role.mode_unknown",
                          f"Garuda does not know what {plan.harness_mode!r} allows on "
                          f"{plan.runtime_id}, so it cannot check it against the role")
    if LEVELS.index(needed) > LEVELS.index(limit):
        where = "a no-edits step" if limit == "readonly" and ceiling != "readonly" else (
            f"permissions: {ceiling}")
        raise RoleRefused("role.mode_exceeds_permissions",
                          f"{plan.harness_mode} on {plan.runtime_id} needs permissions: "
                          f"{needed} or higher, more than {where} allows")
    return plan.harness_mode


def acp_options(plan: RolePlan, adapter_version: str, *, no_edits: bool = False) -> dict[str, str]:
    """The ``session/set_config_option`` settings for an ACP role, or a refusal.

    A mode the role names needs a proven adapter like any option; a default
    mode is applied only where modes are proven.
    """
    wanted = {k: v for k, v in (("model_id", plan.model_id), ("effort", plan.effort)) if v}
    proven = PROVEN_OPTIONS.get((plan.runtime_id, adapter_version))
    mode = harness_mode(plan, no_edits=no_edits)
    if mode is not None and (plan.harness_mode is not None
                             or "harness_mode" in (proven or {})):
        wanted["harness_mode"] = mode
    if not wanted:
        return {}
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
