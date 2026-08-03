"""Run modes: the one knob that picks a gate posture.

``AgentConfig`` carries dozens of independent switches. That is the right shape
for ablation — every gate can be toggled in isolation — but it is the wrong
*interface*, because it makes "just run this task" a research decision. A new
user should not have to know what an acceptance contract is to get a cheap run,
and an eval operator should not have to remember five flags to get a strict one.

So the switches stay, and one selector implies a coherent set of them:

``interactive``
    The default. No gate that costs a model call is on. A run pays for the work
    itself plus the cheap local checks (post-edit syntax/lint, env bootstrap) and
    nothing else.

``eval``
    Every completion gate on: LLM judge, acceptance contract, discriminating
    evidence, stable re-verification, side-effect sweep. This is the posture the
    benchmark numbers were produced under. Roughly two extra model calls per
    completion attempt plus a re-run of each discriminating check — deliberate,
    and worth it when a graded pass is the product.

``rigorous``
    ``eval`` plus the plan → execute → critic agent (see ``core.rigorous``).
    The strictest posture available.

``readonly``
    ``interactive`` gates, and permissions forced read-only. Previously accepted
    as a ``--mode`` value while nothing read it, so it silently ran a normal
    read-write agent; the preset is what gives it effect.

``standard``
    Back-compat alias for ``interactive``. Every default profile ships
    ``mode: standard``, and it was the CLI default, so it has to keep resolving.
    Note this *changes* what those profiles do: `standard` used to inherit the
    full gate stack from ``AgentConfig``'s defaults. That flip is the point —
    the strict posture is now opt-in via ``eval``.

Precedence, widest to narrowest: ``AgentConfig`` defaults, then the mode preset,
then any field the profile YAML *explicitly* declared, then explicit CLI flags.
A profile that says ``enable_acceptance_contract: true`` means it, so the preset
does not overwrite it — which is why ``apply_mode_preset`` takes the set of
declared field names rather than diffing against defaults (a profile declaring a
value that happens to equal the default is still an authored choice).
"""

from __future__ import annotations

from garuda.types import AgentConfig

# Gates that cost a model call or re-run commands. These are what a mode decides;
# cheap local checks (post_edit_diagnostics, bootstrap_environment) are not here
# because there is no posture in which paying for them is a bad trade.
GATE_FIELDS = (
    "enable_llm_verifier",
    "enable_acceptance_contract",
    "require_discriminating_evidence",
    "require_stable_verification",
    "enable_side_effect_sweep",
)

_ALL_GATES_OFF = dict.fromkeys(GATE_FIELDS, False)
_ALL_GATES_ON = dict.fromkeys(GATE_FIELDS, True)

# Fields a preset applies even when the profile declared them. Everything else in
# a preset yields to an authored profile value (see `apply_mode_preset`), which is
# right for the gate switches: a profile asking for the acceptance contract means
# it, and a posture should not silently strip it.
#
# `permission_mode` is different in kind. It is not one of the knobs `readonly`
# adjusts — it is the *entire content* of that posture, and every shipped profile
# declares one (`build: smart`, `harbor: yolo`). Protecting it made `--mode
# readonly` a no-op on the default profile and, worse, left `--mode readonly
# --agent harbor` running with `yolo`: the user asked for "no writes" and got
# "allow everything". A safety posture the target can opt out of is not a posture.
# An explicit `--permission-mode` still wins, because the CLI applies flags after
# the preset — narrower intent, stated later.
FORCED_FIELDS = frozenset({"permission_mode"})

MODE_PRESETS: dict[str, dict[str, object]] = {
    # enable_verifier stays on even here: it is the structural completion gate
    # (task_complete must carry evidence), not a model call. Turning it off is an
    # ablation, not a posture.
    "interactive": {**_ALL_GATES_OFF, "enable_verifier": True},
    "eval": {**_ALL_GATES_ON, "enable_verifier": True},
    "rigorous": {**_ALL_GATES_ON, "enable_verifier": True},
    "readonly": {
        **_ALL_GATES_OFF,
        "enable_verifier": True,
        "permission_mode": "readonly",
    },
}

# Canonical names plus the aliases that must keep resolving.
MODE_ALIASES = {"standard": "interactive"}

# What `--mode` accepts. Ordered for help text: postures first, then the alias.
MODE_CHOICES = ("interactive", "eval", "rigorous", "readonly", "standard")

DEFAULT_MODE = "interactive"


def resolve_mode(mode: str | None) -> str:
    """Map a user-supplied mode (or alias) to its canonical name."""
    if not mode:
        return DEFAULT_MODE
    return MODE_ALIASES.get(mode, mode)


def is_rigorous(mode: str | None) -> bool:
    """Whether this mode routes to the plan/execute/critic agent."""
    return resolve_mode(mode) == "rigorous"


def apply_mode_preset(
    config: AgentConfig,
    mode: str | None = None,
    declared_fields: set[str] | None = None,
) -> AgentConfig:
    """Apply a mode's preset to ``config`` in place and return it.

    ``declared_fields`` names the fields a profile set explicitly; those are left
    alone so the preset never overrides authored intent — except for
    ``FORCED_FIELDS``, which a posture owns outright. An unknown mode applies
    no preset — it is passed through so the caller's own validation reports it,
    rather than being silently coerced to a posture the user did not ask for.
    """
    resolved = resolve_mode(mode if mode is not None else config.mode)
    config.mode = resolved
    preset = MODE_PRESETS.get(resolved)
    if not preset:
        return config
    protected = (declared_fields or set()) - FORCED_FIELDS
    for field, value in preset.items():
        if field not in protected:
            setattr(config, field, value)
    return config


def describe_mode(mode: str | None) -> str:
    """One-line summary of what a mode turns on, for `--help` and run banners."""
    resolved = resolve_mode(mode)
    gates = MODE_PRESETS.get(resolved, {})
    on = [f for f in GATE_FIELDS if gates.get(f)]
    if resolved == "rigorous":
        return "plan/execute/critic agent, all completion gates on"
    if not on:
        note = ", permissions read-only" if resolved == "readonly" else ""
        return f"no model-call gates (cheapest){note}"
    return f"all completion gates on ({len(on)} gates, ~2 extra model calls per attempt)"
