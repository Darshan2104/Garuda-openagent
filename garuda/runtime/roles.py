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
  on a proven adapter. Garuda never selects a mode that bypasses approval by
  itself. Whatever was set, the mode the agent reports after ``session/new``
  and every set must need no more than the authority the run requires
  (:func:`required_authority`, :func:`check_mode`). This holds for every ACP
  harness, not only those Garuda can set a mode on (#700, #732): a broader
  mode, a mode Garuda cannot classify, a harness whose modes it does not know,
  or an agent that reports no mode refuses before the prompt, unless the run
  requires ``yolo``, the top of the scale, which no mode can exceed.

The chosen ids and the adapter identity are recorded on the session.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from garuda.config.garuda_yaml import PERMISSIONS

if TYPE_CHECKING:
    from garuda.agents.spec_api import AgentSpec
    from garuda.runtime.registry import RuntimeRegistry

#: (runtime id, adapter version) -> {role field: ACP config option id}, from
#: the A.3 captures in tests/fixtures/acp/ where `session/set_config_option`
#: was exercised and accepted.
PROVEN_OPTIONS: dict[tuple[str, str], dict[str, str]] = {
    ("claude", "0.85.0"): {"model_id": "model", "effort": "effort", "harness_mode": "mode"},
    ("claude", "0.110.1"): {"model_id": "model", "effort": "effort", "harness_mode": "mode"},
    ("codex", "2.1.1"): {"model_id": "model", "effort": "reasoning_effort",
                         "harness_mode": "mode"},
    ("codex", "2.9.0"): {"model_id": "model", "effort": "reasoning_effort",
                         "harness_mode": "mode"},
}

#: Each harness mode and the lowest ceiling that may select it, from the
#: adapters' own descriptions (tests/fixtures/acp/). A mode that cannot write
#: needs ``readonly``; one that edits the workspace and asks before anything
#: else, ``smart``; one where the agent's own judgement decides what to ask
#: (Claude Code ``auto``, Codex ``agent``), ``auto``; one that approves
#: everything, ``yolo``.
MODE_AUTHORITY: dict[str, dict[str, str]] = {
    "claude": {"default": "readonly", "plan": "readonly", "acceptEdits": "smart",
               "auto": "auto", "bypassPermissions": "yolo"},
    "codex": {"read-only": "readonly", "workspace-write": "smart", "agent": "auto",
              "agent-full-access": "yolo"},
    # OpenCode's ACP modes are its primary agents (captured from opencode 2.3.2 with
    # initialize and session/new only, #732). `build` "executes tools based on
    # configured permissions": the user's OpenCode permission rules decide what asks,
    # and by default edits, shell and access outside the workspace do not. `plan`
    # denies edits outside OpenCode's plan directory, but its shell commands follow
    # the same rules, so it can still write. Neither approves by mode regardless of
    # those rules, so both are `auto`; neither is readonly or smart.
    "opencode": {"build": "auto", "plan": "auto"},
}
#: The mode a role gets when it names none, by the most its step may do. Never
#: a mode that bypasses approval.
DEFAULT_MODES: dict[str, dict[str, str]] = {
    "claude": {"readonly": "default", "smart": "acceptEdits", "auto": "auto", "yolo": "auto"},
    "codex": {"readonly": "read-only", "smart": "workspace-write", "auto": "agent",
              "yolo": "agent"},
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
    #: An explicit agent permission ceiling requires a proven mode or read-only confinement.
    agent_permission_mode: str | None = None
    #: Native resolved source held for execution only; never serialized in record().
    agent_spec: AgentSpec | None = field(default=None, repr=False, compare=False)

    #: Start-time trusted alias/disablement evidence, never persisted or compared.
    #: Review preflight uses this same non-executing registry as role resolution.
    identity_registry: RuntimeRegistry | None = field(default=None, repr=False, compare=False)

    def require_workspace_kind(self, workspace_kind: str) -> None:
        """Check the CLI workspace kind, independent of ACP's own confinement."""
        if self.kind == "acp" and workspace_kind != "local":
            raise RoleRefused(
                "role.workspace_unsupported",
                f"role {self.role} runs on {self.runtime_id}, "
                f"which cannot run in a {workspace_kind} workspace",
            )

    def record(self, *, adapter: dict | None = None, options: dict | None = None,
               mode: dict | None = None) -> dict:
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
        if mode is not None:
            # The mode the agent reported before the prompt, and who chose it.
            out["effective_mode"] = mode
        return out


def check_model_allowed(config: dict, registry, harness: str, model_id: str | None) -> None:
    """Check both reference-local and canonical model restrictions without probes."""
    canonical = registry.get(harness).runtime_id
    for reference in dict.fromkeys((harness, canonical)):
        allowed = config.get("harnesses", {}).get(reference, {}).get("allowed_models")
        if allowed is not None and model_id and model_id not in allowed:
            raise RoleRefused("role.model_not_allowed",
                              f"{model_id} is not in harnesses.{reference}.allowed_models")


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
    if kind == "native" and resolved.role in resolved.external_project_models:
        raise RoleRefused("config.project_untrusted",
                          f"role {resolved.role} now resolves to native; reload the project "
                          "configuration before using its model")
    check_model_allowed(resolved.config, catalog.registry, spec["harness"], spec.get("model_id"))
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


def acp_permission(plan: RolePlan | None = None, *, mode: str | None = None,
                   no_edits: bool = False, ceiling: str | None = None) -> str:
    """Resolve ACP launch authority from the strictest run and role settings."""
    if mode == "readonly" or no_edits or (plan and plan.write_policy == "no-edits"):
        return "readonly"
    return min((plan.permissions if plan else None) or "smart", ceiling or "yolo",
               key=PERMISSIONS.index)


def required_authority(plan: RolePlan, *, no_edits: bool = False,
                       ceiling: str | None = None) -> str:
    """The most the agent's reported mode may need for ``plan`` (#732).

    The authority of the mode the run selects (a named mode, or the default
    for its ceiling, which never bypasses approval) on a harness whose modes
    Garuda knows; otherwise the run's own ceiling. Every ACP run has one.
    """
    mode = harness_mode(plan, no_edits=no_edits, ceiling=ceiling)
    known = MODE_AUTHORITY.get(plan.runtime_id, {})
    if mode is not None and mode in known:
        return known[mode]
    return acp_permission(plan, no_edits=no_edits, ceiling=min(
        plan.permissions or "smart", ceiling or "yolo", key=PERMISSIONS.index))


def harness_mode(plan: RolePlan, *, no_edits: bool = False,
                 ceiling: str | None = None) -> str | None:
    """The harness permission mode to select for ``plan``, or ``None`` to leave
    the adapter's own; refuses a mode the role or step may not use.

    ``ceiling`` is the run's own permission mode when it is stricter than the role's.
    """
    ceiling = min(plan.permissions or "smart", ceiling or "yolo", key=PERMISSIONS.index)
    limit = acp_permission(plan, no_edits=no_edits, ceiling=ceiling)
    if plan.harness_mode is None:
        return DEFAULT_MODES.get(plan.runtime_id, {}).get(limit)
    needed = MODE_AUTHORITY.get(plan.runtime_id, {}).get(plan.harness_mode)
    if needed is None:
        raise RoleRefused("role.mode_unknown",
                          f"Garuda does not know what {plan.harness_mode!r} allows on "
                          f"{plan.runtime_id}, so it cannot check it against the role")
    if PERMISSIONS.index(needed) > PERMISSIONS.index(limit):
        where = "a no-edits step" if limit == "readonly" and ceiling != "readonly" else (
            f"permissions: {ceiling}")
        raise RoleRefused("role.mode_exceeds_permissions",
                          f"{plan.harness_mode} on {plan.runtime_id} needs permissions: "
                          f"{needed} or higher, more than {where} allows")
    return plan.harness_mode


def acp_options(plan: RolePlan, adapter_version: str, *, no_edits: bool = False,
                ceiling: str | None = None) -> dict[str, str]:
    """The ``session/set_config_option`` settings for an ACP role, or a refusal.

    Named modes and known readonly modes require a proven adapter. Other
    default modes are applied only where modes are proven.
    """
    wanted = {k: v for k, v in (("model_id", plan.model_id), ("effort", plan.effort)) if v}
    proven = PROVEN_OPTIONS.get((plan.runtime_id, adapter_version))
    mode = harness_mode(plan, no_edits=no_edits, ceiling=ceiling)
    if (plan.agent_permission_mode is not None
            and (mode is None or "harness_mode" not in (proven or {}))
            and acp_permission(plan, no_edits=no_edits, ceiling=ceiling) != "readonly"):
        raise RoleRefused("agent.field_unsupported",
                          f"role {plan.role} runs on {plan.runtime_id} {adapter_version}, which "
                          f"cannot honour permissions.mode from agent {plan.profile!r}")
    if mode is not None and (plan.harness_mode is not None
                             or acp_permission(plan, no_edits=no_edits, ceiling=ceiling) == "readonly"
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


def check_provable(plan: RolePlan) -> None:
    """Refuse options no proven adapter version of ``plan``'s harness can set. For a
    harness whose version is known only at launch, this is all a check that runs
    nothing can say; launch still checks the exact version."""
    wanted = [k for k in ("model_id", "effort", "harness_mode") if getattr(plan, k)]
    proven = [options for (runtime, _version), options in PROVEN_OPTIONS.items()
              if runtime == plan.runtime_id]
    if wanted and not any(all(k in options for k in wanted) for options in proven):
        raise RoleRefused("role.options_unproven",
                          f"setting {', '.join(wanted)} on {plan.runtime_id} is not proven "
                          "for any adapter version; remove them from the role or use a "
                          "proven adapter")


def check_offered(options: dict[str, str], offered: list, *, model: str | None = None,
                  runtime_id: str = "the agent") -> None:
    """Every requested value must be one the agent offers this session.

    The model is checked first: the other options (effort, mode) depend on it,
    so a model the agent no longer offers is the cause to report (#694).
    ``model`` names the role's model in the other options' refusals.
    """
    by_id = {o.get("id"): o for o in offered or [] if isinstance(o, dict)}
    for config_id, value in sorted(options.items(), key=lambda item: item[0] != "model"):
        option = by_id.get(config_id)
        values = [v.get("value") for v in (option or {}).get("options") or []
                  if isinstance(v, dict)]
        listed = ", ".join(map(str, values)) or "none"
        if config_id == "model":
            if value not in values:
                raise RoleRefused("role.model_unavailable",
                                  f"{runtime_id} does not offer model {value!r} this session "
                                  f"(offered: {listed})")
            continue
        for_model = f" for model {model!r}" if model else ""
        if option is None:
            raise RoleRefused("role.option_missing",
                              f"the agent offers no {config_id!r} option{for_model} this session")
        if value not in values:
            raise RoleRefused("role.option_unavailable",
                              f"{value!r} is not among the agent's {config_id} values"
                              f"{for_model} ({listed}){_mode_hint(runtime_id, config_id, values)}")


def _mode_hint(runtime_id: str, config_id: str, modes: list) -> str:
    """For a mode the agent did not take: the permissions that fit what it offers."""
    authority = MODE_AUTHORITY.get(runtime_id, {})
    if config_id != PROVEN_MODE_OPTION or not authority:
        return ""
    fits = [(mode, authority[mode]) for mode in modes if mode in authority
            and authority[mode] != "yolo"]
    if not fits:
        return ""
    mode, level = max(fits, key=lambda fit: PERMISSIONS.index(fit[1]))
    return f"; use permissions: {level} (mode {mode}) or another model"


def not_applied(runtime_id: str, config_id: str, value: str, now, *,
                model: str | None = None) -> RoleRefused:
    """The refusal for an option the agent did not take, with the likely cause."""
    message = f"the agent reports {config_id}={now!r} after setting {value!r}"
    if config_id == PROVEN_MODE_OPTION and model:
        message += f": model {model!r} does not support mode {value!r} on {runtime_id}"
    if config_id == PROVEN_MODE_OPTION and isinstance(now, str):
        message += _mode_hint(runtime_id, config_id, [now])
    return RoleRefused("role.option_not_applied", message)


#: The ACP config option that holds the harness mode on every proven adapter.
PROVEN_MODE_OPTION = "mode"


def reported_mode(options: list, modes: dict | None) -> str | None:
    """The permission mode an agent reports: its ``mode`` option, else ``modes``."""
    for option in options or []:
        if isinstance(option, dict) and option.get("id") == PROVEN_MODE_OPTION:
            value = option.get("currentValue")
            return value if isinstance(value, str) else None
    current = (modes or {}).get("currentModeId")
    return current if isinstance(current, str) else None


def check_mode(runtime_id: str, required: str, reported: str | None, *,
               adapter_version: str, permission: str, set_by_garuda: bool,
               confined: bool = False, declared: dict[str, str] | None = None) -> dict:
    """Refuse unless the agent's reported mode needs no more than ``required``.

    ``required`` is the run's authority (:func:`required_authority`). Independent
    of proofs (#700) and of the harness (#732): a broader mode, a mode Garuda
    does not know, a harness whose modes it does not know, or no reported mode
    refuses before the prompt. Two cases accept a mode Garuda cannot classify:

    - the run requires ``yolo``, the top of the scale: no mode can do more than
      the run explicitly allows;
    - a readonly run in its Docker confinement (``confined``) on a harness with no
      mode known to be readonly: the read-only mount, not the mode, is what
      stops writes, as for every confined run.

    ``declared`` is the classification a trusted global ``runtimes:`` entry gives
    its adapter's modes (``modes:``); it is used only where Garuda has no table
    for ``runtime_id``, and the record says so (``classified_by: runtimes``).

    Returns what the session records as ``acp_mode``.
    """
    known = MODE_AUTHORITY.get(runtime_id)
    by_user = known is None and bool(declared)
    if by_user:
        known = dict(declared)
    actual = known.get(reported) if known is not None and isinstance(reported, str) else None
    record = {"mode": reported, "authority": actual, "required": required,
              "source": "garuda" if set_by_garuda else "agent"}
    if by_user:
        record["classified_by"] = "runtimes"
    if actual is not None and PERMISSIONS.index(actual) <= PERMISSIONS.index(required):
        return record
    if required == PERMISSIONS[-1]:
        return record  # nothing is broader than yolo, which the run asked for
    if (confined and required == "readonly"
            and "readonly" not in (known or {}).values()):
        return {**record, "enforced_by": "confinement"}
    if reported is None:
        found = "reports no permission mode"
    elif known is None:
        found = (f"runs in mode {reported!r}, and Garuda does not know what "
                 f"{runtime_id}'s modes allow")
    elif actual is None:
        found = f"runs in mode {reported!r}, which Garuda does not know"
    else:
        found = f"runs in mode {reported!r}, which needs permissions: {actual}"
    proven = sorted(version for (runtime, version), options in PROVEN_OPTIONS.items()
                    if runtime == runtime_id and "harness_mode" in options)
    if set_by_garuda or "harness_mode" in PROVEN_OPTIONS.get((runtime_id, adapter_version), {}):
        remedy = ""
    elif proven:
        remedy = (f"; Garuda cannot set the mode on this adapter version, so use a proven "
                  f"one ({', '.join(proven)})")
    elif actual is not None:
        remedy = (f"; Garuda cannot set a mode on {runtime_id}, so this harness needs "
                  f"permissions: {actual} or higher")
    else:
        remedy = (f"; Garuda cannot classify this mode, so only permissions: "
                  f"{PERMISSIONS[-1]} runs {runtime_id} in it, unless its trusted "
                  "runtimes: entry classifies it (modes:)")
    raise RoleRefused("role.mode_unenforced",
                      f"{runtime_id} {adapter_version} {found}; this run allows "
                      f"permissions: {required} at most (run permissions: {permission})"
                      f"{remedy}")
