"""One additive ``garuda.yaml``: roles, harnesses, flows, checks (plan task C.1, #158).

Two files, both optional, neither required for anything that works today:

- the **user** file next to ``global_settings_path()`` (normally
  ``~/.agent/garuda.yaml``) — authority ``user-config``;
- the **project** file ``garuda.yaml`` at the repository root — authority
  ``trusted-project`` (C.2 binds it to a hash-bound trust record before any
  of its commands, models or prompts are used).

Authority comes only from which file a value was read from. An ``authority``
key anywhere is rejected.

**Parsing** is safe YAML (no object tags), refuses duplicate keys, unknown
keys and out-of-range limits with the value's full path (``roles.coder.effort``),
and enforces the v1 bounds: 32 steps per flow, 16 parallel reviewers, 10 coder
retries, 100 consults per root task. A flow step cannot be a flow.
``write_policy: no-edits`` is its own field, never a permission mode, and a
parallel group cannot be ``no-edits``.

**Layering** (:func:`resolve`) — package default < user < trusted project <
CLI request:

- the selected role is the CLI's, else (unless a legacy ``--runtime`` /
  ``--model`` flag was given) the nearest ``defaults.role``;
- a named role or flow replaces the whole definition below it;
- permission ceilings and consult limits intersect (the stricter wins);
- checks accumulate, deduplicated on run text, cwd, environment and mode;
- ``harnesses``, ``sessions.keep_days``, ``fallback`` chains and ``consult``
  grants are **user-only**: a project may keep a subset or remove them, never
  add (``config.project_widening``);
- a legacy settings key in a ``garuda.yaml``, a ``max_parallel`` that
  disagrees with the legacy ``capacity`` setting, or a CLI runtime or model
  that contradicts the selected role is ``config.conflict``.

Every resolved leaf carries its provenance. Applying a role to a run arrives
with exact role resolution (C.3), after project trust (C.2).
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from garuda.config_sources import read_source_text
from garuda.model.config import ConfigError

VERSION = 1
MAX_STEPS = 32
MAX_PARALLEL_REVIEWERS = 16
MAX_RETRIES = 10
MAX_CONSULTS = 100
#: Bounds of `harnesses.<id>.handshake_timeout_sec`, the ACP startup deadline.
MIN_HANDSHAKE_SEC = 5
MAX_HANDSHAKE_SEC = 600
PERMISSIONS = ("readonly", "smart", "auto", "yolo")  # most to least restrictive
EFFORTS = ("minimal", "low", "medium", "high")
WRITE_POLICIES = ("edits", "no-edits")
#: A step's liveness limits in minutes (#828): ``deadline_min`` up to 7 days,
#: ``silence_min`` and ``tool_silence_min`` up to one day.
STEP_LIMITS = {"deadline_min": (1, 7 * 24 * 60), "silence_min": (1, 24 * 60),
               "tool_silence_min": (1, 24 * 60)}
#: A long step's turn loop (``long_run``): whole hours and turns, with their bounds.
#: Absent keys take :data:`garuda.flows.long_step.DEFAULTS`.
LONG_RUN = {"max_hours": (1, 24), "session_turns": (1, 200), "session_hours": (1, 24),
            "max_turns": (1, 1000), "no_progress_turns": (1, 50)}
ISOLATION = ("shared", "worktree", "auto")
ARTIFACTS = ("plan", "patch", "review", "findings", "notes", "summary")
#: Keys that belong to ``settings.yaml``; in a ``garuda.yaml`` they conflict.
LEGACY_KEYS = ("runtimes", "disabled_runtimes", "runtime_refs", "routing", "models",
               "model_bindings", "model_bindings_default", "model_binding", "collection",
               "capacity", "agents", "hooks", "load_project_tools", "trust_project_hooks",
               "mcp_merge")

USER, PROJECT, CLI, PACKAGE = "user-config", "trusted-project", "user-request", "package-default"


class GarudaConfigError(ConfigError):
    """A refused configuration. ``code`` is stable; ``path`` names the value."""

    def __init__(self, code: str, path: str, message: str):
        super().__init__(f"{code}: {path or '(file)'}: {message}")
        self.code = code
        self.path = path


def _fail(path: str, message: str, code: str = "config.invalid"):
    raise GarudaConfigError(code, path, message)


# --- parsing ---------------------------------------------------------------------


def _mapping(value, path, allowed: tuple[str, ...]) -> dict:
    if not isinstance(value, dict):
        _fail(path, "must be a mapping")
    for key in value:
        if not isinstance(key, str):
            _fail(path, f"keys must be strings, got {key!r}")
        if key == "authority":
            _fail(f"{path}.{key}".lstrip("."), "authority comes from the file, never a key")
        if key not in allowed:
            _fail(f"{path}.{key}".lstrip("."), f"unknown key (allowed: {', '.join(allowed)})")
    return value


def _str(value, path) -> str:
    if not isinstance(value, str) or not value.strip():
        _fail(path, "must be a non-empty string")
    return value


def _name(value, path) -> str:
    import re

    value = _str(value, path)
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,47}", value):
        _fail(path, f"{value!r} is not a name (lowercase letters, digits, '-', '_')")
    return value


def _int(value, path, lo: int, hi: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
        _fail(path, f"must be an integer from {lo} to {hi}")
    return value


def _choice(value, path, choices) -> str:
    if value not in choices:
        _fail(path, f"must be one of {', '.join(choices)}")
    return value


def _names(value, path) -> list[str]:
    if not isinstance(value, list):
        _fail(path, "must be a list")
    out = [_name(v, f"{path}[{i}]") for i, v in enumerate(value)]
    if len(set(out)) != len(out):
        _fail(path, "has duplicates")
    return out


def _strs(value, path) -> list[str]:
    if not isinstance(value, list):
        _fail(path, "must be a list")
    return [_str(v, f"{path}[{i}]") for i, v in enumerate(value)]


def _confinement(value, path) -> dict:
    data = _mapping(value, path, ("image", "command", "mounts"))
    out: dict[str, Any] = {"image": _str(data.get("image"), f"{path}.image")}
    if "command" in data:
        out["command"] = _strs(data["command"], f"{path}.command")
    if "mounts" in data:
        mounts = _strs(data["mounts"], f"{path}.mounts")
        for i, mount in enumerate(mounts):
            if not mount.startswith("/"):
                _fail(f"{path}.mounts[{i}]", "must be an absolute path (mounted read-only)")
        out["mounts"] = mounts
    return out


def _harness(value, path) -> dict:
    data = _mapping(value or {}, path, ("allowed_models", "max_parallel", "confinement",
                                        "handshake_timeout_sec"))
    out: dict[str, Any] = {}
    if "confinement" in data:
        out["confinement"] = _confinement(data["confinement"], f"{path}.confinement")
    if "allowed_models" in data:
        out["allowed_models"] = _strs(data["allowed_models"], f"{path}.allowed_models")
    if "max_parallel" in data:
        out["max_parallel"] = _int(data["max_parallel"], f"{path}.max_parallel", 1, 64)
    if "handshake_timeout_sec" in data:
        out["handshake_timeout_sec"] = _int(data["handshake_timeout_sec"],
                                            f"{path}.handshake_timeout_sec",
                                            MIN_HANDSHAKE_SEC, MAX_HANDSHAKE_SEC)
    return out


def _candidate(value, path) -> dict:
    data = _mapping(value, path, ("harness", "model_id"))
    out = {"harness": _name(data.get("harness"), f"{path}.harness")}
    if "model_id" in data:
        out["model_id"] = _str(data["model_id"], f"{path}.model_id")
    return out


def _role(value, path) -> dict:
    data = _mapping(value, path, ("harness", "model_id", "effort", "permissions", "write_policy",
                                  "profile", "agent", "fallback", "consult", "description",
                                  "harness_mode"))
    out: dict[str, Any] = {"harness": _name(data.get("harness"), f"{path}.harness")}
    if "model_id" in data:
        out["model_id"] = _str(data["model_id"], f"{path}.model_id")
    if "effort" in data:
        out["effort"] = _choice(data["effort"], f"{path}.effort", EFFORTS)
    if "permissions" in data:
        out["permissions"] = _choice(data["permissions"], f"{path}.permissions", PERMISSIONS)
    if "write_policy" in data:
        out["write_policy"] = _choice(data["write_policy"], f"{path}.write_policy",
                                      WRITE_POLICIES)
    if "profile" in data:
        out["profile"] = _name(data["profile"], f"{path}.profile")
    if "agent" in data:  # the agent definition the role runs under (H.10); same field as `profile`
        agent = _name(data["agent"], f"{path}.agent")
        if "profile" in out and out["profile"] != agent:
            _fail(f"{path}.agent", f"conflicts with {path}.profile; name one agent",
                  code="config.conflict")
        out["profile"] = agent
    if "description" in data:
        out["description"] = _str(data["description"], f"{path}.description")
    if "harness_mode" in data:  # the harness's own permission mode, checked per harness
        out["harness_mode"] = _str(data["harness_mode"], f"{path}.harness_mode")
    if "fallback" in data:
        if not isinstance(data["fallback"], list):
            _fail(f"{path}.fallback", "must be a list")
        out["fallback"] = [_candidate(c, f"{path}.fallback[{i}]")
                           for i, c in enumerate(data["fallback"])]
    if "consult" in data:
        out["consult"] = _names(data["consult"], f"{path}.consult")
    return out


def _artifacts(value, path) -> list[str]:
    if not isinstance(value, list):
        _fail(path, "must be a list")
    return [_choice(v, f"{path}[{i}]", ARTIFACTS) for i, v in enumerate(value)]


def _input(value, path) -> str | dict:
    """One ``inputs`` entry: a type, or ``{type: T, from: STEP}`` (#817).

    A plain type stays the plain string it always was, so the normalized
    definition (and its digest) of a flow without the mapping form is
    unchanged. ``from`` is a step id, or ``GROUP:ROLE`` for one member of a
    parallel group (the same ``:`` naming as the member's launch id).
    """
    if not isinstance(value, dict):
        return _choice(value, path, ARTIFACTS)
    data = _mapping(value, path, ("type", "from"))
    if "type" not in data:
        _fail(f"{path}.type", "is required")
    if "from" not in data:
        _fail(f"{path}.from", "is required (a plain type takes the latest earlier producer)")
    kind = _choice(data["type"], f"{path}.type", ARTIFACTS)
    source = _str(data["from"], f"{path}.from")
    step, _, member = source.partition(":")
    _name(step, f"{path}.from")
    if ":" in source:
        _name(member, f"{path}.from")
    return {"type": kind, "from": source}


def input_source(entry: str | dict) -> tuple[str, str | None, str | None]:
    """``(type, step, member)`` of an ``inputs`` entry; step is None for a plain type."""
    if isinstance(entry, str):
        return entry, None, None
    step, _, member = entry["from"].partition(":")
    return entry["type"], step, member or None


def _inputs(value, path) -> list:
    if not isinstance(value, list):
        _fail(path, "must be a list")
    return [_input(v, f"{path}[{i}]") for i, v in enumerate(value)]


def _step(value, path) -> dict:
    if isinstance(value, dict) and "flow" in value:
        _fail(f"{path}.flow", "a step cannot be a flow (nested flows are not supported)")
    data = _mapping(value, path, ("id", "role", "parallel", "write_policy", "inputs", "outputs",
                                  "retries", "review", "limits", "long_run"))
    out: dict[str, Any] = {}
    if "id" in data:
        out["id"] = _name(data["id"], f"{path}.id")
    if ("role" in data) == ("parallel" in data):
        _fail(path, "a step has exactly one of role or parallel")
    if "role" in data:
        out["role"] = _name(data["role"], f"{path}.role")
    else:
        group = data["parallel"]
        if not isinstance(group, list) or not group:
            _fail(f"{path}.parallel", "must be a non-empty list of roles")
        if len(group) > MAX_PARALLEL_REVIEWERS:
            _fail(f"{path}.parallel", f"at most {MAX_PARALLEL_REVIEWERS} parallel reviewers")
        out["parallel"] = _names(group, f"{path}.parallel")
    if "write_policy" in data:
        out["write_policy"] = _choice(data["write_policy"], f"{path}.write_policy",
                                      WRITE_POLICIES)
        if "parallel" in out and out["write_policy"] == "no-edits":
            _fail(f"{path}.write_policy", "a parallel group cannot be no-edits")
    if "inputs" in data:
        out["inputs"] = _inputs(data["inputs"], f"{path}.inputs")
    if "outputs" in data:
        out["outputs"] = _artifacts(data["outputs"], f"{path}.outputs")
    if "retries" in data:
        out["retries"] = _int(data["retries"], f"{path}.retries", 0, MAX_RETRIES)
    if "limits" in data:
        out["limits"] = _limits(data["limits"], f"{path}.limits")
    if "long_run" in data:
        if "parallel" in out:
            _fail(f"{path}.long_run", "a parallel group cannot run long; give a single role step "
                  "long_run")
        out["long_run"] = _long_run(data["long_run"], f"{path}.long_run")
    if "review" in data:
        review = _mapping(data["review"], f"{path}.review", ("by", "max_rounds", "independent"))
        out["review"] = {"by": _name(review.get("by"), f"{path}.review.by")}
        if "max_rounds" in review:
            out["review"]["max_rounds"] = _int(review["max_rounds"], f"{path}.review.max_rounds",
                                               0, MAX_RETRIES)
        if "independent" in review:
            if not isinstance(review["independent"], bool):
                _fail(f"{path}.review.independent", "must be true or false")
            out["review"]["independent"] = review["independent"]
    return out


def _limits(value, path) -> dict:
    """A step's (or a flow's default) liveness limits, whole minutes (#828).

    Present keys only, so a flow without ``limits`` parses (and digests) exactly
    as before. While a tool call runs, ``tool_silence_min`` replaces
    ``silence_min``, so it may not be the shorter of the two.
    """
    data = _mapping(value, path, tuple(STEP_LIMITS))
    if not data:
        _fail(path, f"must set at least one of {', '.join(STEP_LIMITS)}")
    out = {key: _int(data[key], f"{path}.{key}", *STEP_LIMITS[key])
           for key in STEP_LIMITS if key in data}
    _check_silences(out, path)
    return out


def _long_run(value, path) -> dict:
    """A step's (or a flow's default) turn loop (``long_run``): present keys only,
    so a flow without it parses and digests exactly as before. ``{}`` takes every
    default."""
    data = _mapping(value, path, tuple(LONG_RUN))
    out = {key: _int(data[key], f"{path}.{key}", *LONG_RUN[key])
           for key in LONG_RUN if key in data}
    _check_long_run(out, path)
    return out


def _check_long_run(long_run: dict, path: str) -> None:
    if long_run.get("session_hours", 0) > long_run.get("max_hours", 24):
        _fail(f"{path}.session_hours", "must not be longer than max_hours")


def _check_silences(limits: dict, path: str) -> None:
    if limits.get("tool_silence_min", float("inf")) < limits.get("silence_min", 0):
        _fail(f"{path}.tool_silence_min", "must not be shorter than silence_min")


def effective_flow_steps(flow: dict, *, path: str = "flow.steps") -> list[dict]:
    """Freeze the IDs used by admission, execution, receipts and resume."""
    steps = []
    seen = set()
    for index, step in enumerate(flow["steps"]):
        sid = step.get("id") or (step.get("role") or "group") + f"-{index + 1}"
        if sid in seen:
            _fail(path, f"step ids must be unique (duplicate {sid!r})")
        seen.add(sid)
        steps.append({**step, "id": sid, "index": index})
    return steps


def validate_flow_inputs(steps: list[dict], *, path: str) -> None:
    """Every step input must be an output of an earlier step.

    The engine hands a step the latest earlier artifact of each plain input
    kind (every member's, from a parallel group); a kind no earlier step
    declares can never exist, so the flow is refused at load time instead of
    stopping with ``flow.input_missing`` mid-run.

    A ``{type, from}`` entry (#817) names its producer: ``from`` must be an
    earlier step that declares that output, and ``GROUP:ROLE`` must name a
    member of a parallel group. Within one step a type is asked for either
    plainly or by producer, never both, and the same producer (a group or
    one of its members) is named at most once per type, so no artifact is
    delivered twice.
    """
    available: set[str] = set()
    earlier: dict[str, dict] = {}
    for i, step in enumerate(steps):
        plain: set[str] = set()
        named: dict[str, set[str]] = {}
        for j, entry in enumerate(step.get("inputs", [])):
            at = f"{path}[{i}].inputs[{j}]"
            kind, source, member = input_source(entry)
            if source is None:
                if kind not in available:
                    _fail(at, f"no earlier step outputs a {kind}"
                          + (f" (earlier steps output {', '.join(sorted(available))})"
                             if available else " (it is the first step)"))
                if kind in named:
                    _fail(at, f"{kind} is also asked for by producer; use one form per type")
                plain.add(kind)
                continue
            at_from = f"{at}.from"
            producer = earlier.get(source)
            if producer is None:
                later = any(s["id"] == source for s in steps[i:])
                _fail(at_from, f"step {source!r} "
                      + ("is not earlier than this step" if later else "does not exist")
                      + (f" (earlier steps: {', '.join(earlier)})"
                         if earlier else " (it is the first step)"))
            if kind not in producer.get("outputs", []):
                _fail(at_from, f"step {source!r} does not output a {kind}")
            if member is not None and member not in producer.get("parallel", []):
                _fail(at_from, f"{member!r} is not a member of parallel step {source!r}"
                      if "parallel" in producer else f"step {source!r} is not a parallel group")
            if kind in plain:
                _fail(at, f"{kind} is also asked for plainly; use one form per type")
            taken = named.setdefault(kind, set())
            if source in taken or f"{source}:{member}" in taken or (
                    member is None and any(t.startswith(f"{source}:") for t in taken)):
                _fail(at, f"duplicate input: {kind} from {entry['from']!r} is already delivered")
            taken.add(source if member is None else f"{source}:{member}")
        available.update(step.get("outputs", []))
        earlier[step["id"]] = step


def validate_flow_reviews(steps: list[dict], *, path: str) -> None:
    """Validate the terminal review at each point the engine invokes it."""
    available: set[str] = set()
    for i, step in enumerate(steps):
        available.update(step.get("outputs", []))
        review = step.get("review")
        if not review:
            continue
        if "parallel" in step:
            _fail(f"{path}[{i}].review", "a parallel group cannot be reviewed")
        if review["by"] == step.get("role"):
            _fail(f"{path}[{i}].review.by", "a step cannot review itself")
        terminal = steps[-1]
        if i == len(steps) - 1 or terminal.get("role") != review["by"]:
            _fail(f"{path}[{i}].review.by",
                  "must name the flow's terminal reviewer (the role of its last step)")
        terminal_outputs = terminal.get("outputs", [])
        if "review" not in terminal_outputs:
            _fail(f"{path}[{len(steps) - 1}].outputs", "the terminal reviewer must output a review")
        plain = {e for e in terminal.get("inputs", []) if isinstance(e, str)}
        missing = plain - available
        if missing:
            _fail(f"{path}[{len(steps) - 1}].inputs",
                  f"{', '.join(sorted(missing))} must be produced before or by step {step['id']!r}, "
                  "which the terminal reviewer reviews immediately")
        # The mapping form (#817): its producer must run before or be the reviewed step.
        order = {s["id"]: k for k, s in enumerate(steps)}
        for j, entry in enumerate(terminal.get("inputs", [])):
            _, source, _ = input_source(entry)
            if source is not None and order.get(source, len(steps)) > i:
                _fail(f"{path}[{len(steps) - 1}].inputs[{j}].from",
                      f"step {source!r} runs after step {step['id']!r}, "
                      "which the terminal reviewer reviews immediately")
        available.update(terminal_outputs)


def validate_flow_retries(steps: list[dict], *, path: str) -> None:
    """``retries`` applies to plain steps only (#810).

    A reviewed step reruns through ``review.max_rounds``, the terminal reviewer
    of a reviewed flow runs inside that loop, and a parallel group reviews one
    snapshot; none of them is retried, so the key is refused there.
    """
    reviewed = any(step.get("review") for step in steps)
    for i, step in enumerate(steps):
        if "retries" not in step:
            continue
        if "parallel" in step:
            _fail(f"{path}[{i}].retries", "a parallel group cannot be retried")
        if step.get("review"):
            _fail(f"{path}[{i}].retries", "a reviewed step reruns through review.max_rounds")
        if reviewed and i == len(steps) - 1:
            _fail(f"{path}[{i}].retries",
                  "the terminal reviewer of a reviewed flow runs once per review round")


def _flow(value, path) -> dict:
    data = _mapping(value, path, ("steps", "description", "limits", "long_run"))
    steps = data.get("steps")
    if not isinstance(steps, list) or not steps:
        _fail(f"{path}.steps", "must be a non-empty list")
    if len(steps) > MAX_STEPS:
        _fail(f"{path}.steps", f"at most {MAX_STEPS} steps")
    out: dict[str, Any] = {"steps": [_step(s, f"{path}.steps[{i}]") for i, s in enumerate(steps)]}
    steps_path = f"{path}.steps"
    steps = effective_flow_steps(out, path=steps_path)
    validate_flow_inputs(steps, path=steps_path)
    validate_flow_reviews(steps, path=steps_path)
    validate_flow_retries(steps, path=steps_path)
    if "limits" in data:
        out["limits"] = _limits(data["limits"], f"{path}.limits")
    if "long_run" in data:
        out["long_run"] = _long_run(data["long_run"], f"{path}.long_run")
    for i, step in enumerate(out["steps"]):
        if "limits" in step and "limits" in out:  # a step overrides the default per key
            _check_silences({**out["limits"], **step["limits"]}, f"{steps_path}[{i}].limits")
        if "long_run" in step and "long_run" in out:
            _check_long_run({**out["long_run"], **step["long_run"]}, f"{steps_path}[{i}].long_run")
    if "description" in data:
        out["description"] = _str(data["description"], f"{path}.description")
    return out


def _check(value, path) -> dict:
    if isinstance(value, str):
        return {"run": _str(value, path)}
    data = _mapping(value, path, ("run", "cwd", "env", "mode"))
    run = data.get("run")
    if isinstance(run, list):
        out: dict[str, Any] = {"run": _strs(run, f"{path}.run")}
        if not out["run"]:
            _fail(f"{path}.run", "must not be empty")
    else:
        out = {"run": _str(run, f"{path}.run")}
    if "cwd" in data:
        cwd = _str(data["cwd"], f"{path}.cwd")
        if cwd.startswith("/") or ".." in Path(cwd).parts:
            _fail(f"{path}.cwd", "must be a path inside the repository")
        out["cwd"] = cwd
    if "env" in data:
        env = data["env"]
        if not isinstance(env, dict) or not all(isinstance(k, str) and k for k in env):
            _fail(f"{path}.env", "must map variable names to strings")
        out["env"] = {k: _str(v, f"{path}.env.{k}") for k, v in env.items()}
    if "mode" in data:
        out["mode"] = _choice(data["mode"], f"{path}.mode", ("host", "docker"))
    return out


def parse(data: Any, *, source: str = "") -> dict:
    """Validate a parsed ``garuda.yaml`` document; returns its normalized form."""
    if data is None:
        data = {}
    if not isinstance(data, dict):
        _fail("", "the file must hold a mapping")
    for key in data:
        if key in LEGACY_KEYS:
            _fail(key, "is a settings.yaml key; it does not belong in garuda.yaml "
                       "(see `garuda config migrate`)", code="config.conflict")
    _mapping(data, "", ("version", "defaults", "harnesses", "roles", "flows", "checks",
                        "consults", "sessions"))
    if data.get("version") != VERSION:
        _fail("version", f"must be {VERSION}", code="config.unsupported_version")
    out: dict[str, Any] = {"version": VERSION}
    if "defaults" in data:
        defaults = _mapping(data["defaults"], "defaults", ("role",))
        out["defaults"] = {"role": _name(defaults.get("role"), "defaults.role")} if defaults else {}
    if "harnesses" in data:
        harnesses = _mapping(data["harnesses"], "harnesses", tuple(data["harnesses"] or ()))
        out["harnesses"] = {_name(k, f"harnesses.{k}"): _harness(v, f"harnesses.{k}")
                            for k, v in harnesses.items()}
    if "roles" in data:
        roles = _mapping(data["roles"], "roles", tuple(data["roles"] or ()))
        out["roles"] = {_name(k, f"roles.{k}"): _role(v, f"roles.{k}") for k, v in roles.items()}
    if "flows" in data:
        flows = _mapping(data["flows"], "flows", tuple(data["flows"] or ()))
        out["flows"] = {_name(k, f"flows.{k}"): _flow(v, f"flows.{k}") for k, v in flows.items()}
    if "checks" in data:
        if not isinstance(data["checks"], list):
            _fail("checks", "must be a list")
        out["checks"] = [_check(c, f"checks[{i}]") for i, c in enumerate(data["checks"])]
    if "consults" in data:
        consults = _mapping(data["consults"], "consults",
                            ("max_per_session", "timeout_sec", "max_answer_chars",
                             "max_question_chars", "max_brief_tokens", "max_turns",
                             "max_output_tokens"))
        out["consults"] = {}
        if "max_per_session" in consults:
            out["consults"]["max_per_session"] = _int(
                consults["max_per_session"], "consults.max_per_session", 0, MAX_CONSULTS)
        if "timeout_sec" in consults:
            out["consults"]["timeout_sec"] = _int(consults["timeout_sec"],
                                                  "consults.timeout_sec", 1, 3600)
        if "max_answer_chars" in consults:
            out["consults"]["max_answer_chars"] = _int(
                consults["max_answer_chars"], "consults.max_answer_chars", 1, 100_000)
        for key, high in (("max_question_chars", 100_000), ("max_brief_tokens", 32_768),
                          ("max_turns", 50), ("max_output_tokens", 32_768)):
            if key in consults:
                out["consults"][key] = _int(consults[key], f"consults.{key}", 1, high)
    if "sessions" in data:
        sessions = _mapping(data["sessions"], "sessions", ("isolation", "keep_days"))
        out["sessions"] = {}
        if "isolation" in sessions:
            out["sessions"]["isolation"] = _choice(sessions["isolation"], "sessions.isolation",
                                                   ISOLATION)
        if "keep_days" in sessions:
            out["sessions"]["keep_days"] = _int(sessions["keep_days"], "sessions.keep_days",
                                                1, 3650)
    _check_references(out)
    return out


def _check_references(doc: dict) -> None:
    roles = doc.get("roles", {})
    for name, role in roles.items():
        for consulted in role.get("consult", []):
            if consulted == name:
                _fail(f"roles.{name}.consult", "a role cannot consult itself")


def load_text(text: str, *, source: str = "") -> dict:
    """Parse YAML text safely (no tags, unique keys) and validate it."""
    import yaml

    from garuda.agents.frontmatter import load_yaml_unique

    try:
        data = load_yaml_unique(text)
    except yaml.YAMLError as exc:
        _fail("", f"not valid YAML: {exc}")
    return parse(data, source=source)


def dump(doc: dict) -> str:
    """The canonical YAML of a normalized document (round-trips through parse)."""
    import yaml

    return yaml.safe_dump(doc, sort_keys=False, default_flow_style=False, allow_unicode=True)


def user_path() -> Path:
    from garuda.config.agent_home import global_settings_path

    return global_settings_path().expanduser().parent / "garuda.yaml"


def project_path(workspace) -> Path:
    """Where the project file lives; read it through ``project_trust``."""
    from garuda.core.sessions import project_root

    return Path(project_root(str(Path(workspace).resolve()))) / "garuda.yaml"


def load_file(path: Path) -> dict | None:
    try:
        text = read_source_text(path)
    except FileNotFoundError:
        return None
    except OSError as exc:
        _fail("", f"{path} is unreadable: {exc}")
    try:
        return load_text(text, source=str(path))
    except GarudaConfigError as exc:
        raise GarudaConfigError(exc.code, exc.path, f"{path}: {str(exc).split(': ', 2)[-1]}") \
            from exc


# --- layering ----------------------------------------------------------------------


@dataclass
class Resolved:
    """The effective configuration with a source for every leaf."""

    config: dict
    provenance: dict[str, str] = field(default_factory=dict)
    role: str | None = None
    #: Project values left out because the project file is not trusted (C.2).
    withheld: list[str] = field(default_factory=list)
    #: Untrusted project models admitted for external runtimes only.
    external_project_models: frozenset[str] = frozenset()

    def selected_role(self) -> dict | None:
        return self.config.get("roles", {}).get(self.role) if self.role else None


def _stricter(a: str | None, b: str | None) -> str | None:
    if a is None or b is None:
        return a or b
    return a if PERMISSIONS.index(a) <= PERMISSIONS.index(b) else b


def _is_subsequence(part: list, whole: list) -> bool:
    it = iter(whole)
    return all(any(x == y for y in it) for x in part)


def _check_key(check: dict) -> tuple:
    run = check["run"]
    return (tuple(run) if isinstance(run, list) else run, check.get("cwd", "."),
            tuple(sorted((check.get("env") or {}).items())), check.get("mode", "host"))


def _check_project_ceiling(path: str, wanted: str | None, ceiling: str) -> None:
    if wanted and PERMISSIONS.index(wanted) > PERMISSIONS.index(ceiling):
        _fail(f"{path}.permissions", f"{wanted} is above the project ceiling {ceiling} "
              "(agents.project_ceiling in settings.yaml)", code="config.project_widening")


def _narrow_role(name: str, user: dict | None, project: dict, ceiling: str) -> dict:
    path = f"roles.{name}"
    if user is None:
        for key in ("fallback", "consult"):
            if project.get(key):
                _fail(f"{path}.{key}", f"{key} grants are user-only; a project cannot add them",
                      code="config.project_widening")
        _check_project_ceiling(path, project.get("permissions"), ceiling)
        return project
    role = dict(project)
    if user.get("write_policy") == "no-edits":
        role["write_policy"] = "no-edits"
    if user.get("permissions") is None:
        _check_project_ceiling(path, project.get("permissions"), ceiling)
    role["permissions"] = _stricter(user.get("permissions"), project.get("permissions"))
    if role["permissions"] is None:
        role.pop("permissions")
    if "fallback" in project:
        if not _is_subsequence(project["fallback"], user.get("fallback", [])):
            _fail(f"{path}.fallback", "a project may only remove fallback entries",
                  code="config.project_widening")
    elif "fallback" in user:
        role["fallback"] = user["fallback"]
    if "consult" in project:
        if not set(project["consult"]) <= set(user.get("consult", [])):
            _fail(f"{path}.consult", "a project may only remove consult grants",
                  code="config.project_widening")
    elif "consult" in user:
        role["consult"] = user["consult"]
    return role


def _narrow_harnesses(user: dict, project: dict) -> dict:
    out = copy.deepcopy(user)
    for name, spec in project.items():
        path = f"harnesses.{name}"
        if name not in user:
            _fail(path, "harnesses are user-only; a project cannot add one",
                  code="config.project_widening")
        base = out[name]
        if "allowed_models" in spec:
            if "allowed_models" in base and not set(spec["allowed_models"]) <= set(
                    base["allowed_models"]):
                _fail(f"{path}.allowed_models", "a project may only remove models",
                      code="config.project_widening")
            base["allowed_models"] = spec["allowed_models"]
        if "max_parallel" in spec:
            base["max_parallel"] = min(spec["max_parallel"], base.get("max_parallel", 64))
        if "confinement" in spec:
            _fail(f"{path}.confinement", "confinement is user-only",
                  code="config.project_widening")
        if "handshake_timeout_sec" in spec:
            _fail(f"{path}.handshake_timeout_sec", "the ACP startup deadline is user-only",
                  code="config.project_widening")
    return out


def resolve(
    user: dict | None = None,
    project: dict | None = None,
    *,
    cli_role: str | None = None,
    cli_runtime: str | None = None,
    cli_model: str | None = None,
    cli_checks: list[str] = (),
    package: dict | None = None,
    legacy_capacity: dict | None = None,
    project_ceiling: str = "smart",
    files: dict[str, str] | None = None,
) -> Resolved:
    """Layer package < user < project < CLI. See the module docstring.

    ``files`` maps a layer (``USER``, ``PROJECT``) to the file it was read
    from, so a cross-reference error names the file like a parse error does.
    """
    layers = [(PACKAGE, package or {}), (USER, user or {}), (PROJECT, project or {})]
    config: dict[str, Any] = {"version": VERSION}
    prov: dict[str, str] = {}

    # roles and flows: whole-definition replacement, with project narrowing
    roles: dict[str, dict] = {}
    for source, doc in layers:
        for name, role in doc.get("roles", {}).items():
            if source == PROJECT:
                role = _narrow_role(name, roles.get(name), role, project_ceiling)
            roles[name] = role
            prov[f"roles.{name}"] = source
    flows: dict[str, dict] = {}
    for source, doc in layers:
        for name, flow in doc.get("flows", {}).items():
            flows[name] = flow
            prov[f"flows.{name}"] = source

    # user-only: harnesses and sessions.keep_days
    harnesses = copy.deepcopy((package or {}).get("harnesses", {}))
    harnesses.update(copy.deepcopy((user or {}).get("harnesses", {})))
    for name in harnesses:
        prov[f"harnesses.{name}"] = USER if name in (user or {}).get("harnesses", {}) else PACKAGE
    if (project or {}).get("harnesses"):
        harnesses = _narrow_harnesses(harnesses, project["harnesses"])
        for name in project["harnesses"]:
            prov[f"harnesses.{name}"] = PROJECT
    for name, spec in harnesses.items():
        legacy = (legacy_capacity or {}).get(name)
        if legacy is not None and "max_parallel" in spec and spec["max_parallel"] != legacy:
            _fail(f"harnesses.{name}.max_parallel",
                  f"is {spec['max_parallel']} but settings.yaml capacity.{name} is {legacy}",
                  code="config.conflict")

    sessions: dict[str, Any] = {}
    for source, doc in layers:
        for key, value in doc.get("sessions", {}).items():
            if source == PROJECT and key == "keep_days":
                _fail("sessions.keep_days", "is user-only", code="config.project_widening")
            sessions[key] = value
            prov[f"sessions.{key}"] = source

    consults: dict[str, int] = {}
    for source, doc in layers:
        for key, value in doc.get("consults", {}).items():
            if key not in consults or value < consults[key]:  # limits only tighten
                consults[key] = value
                prov[f"consults.{key}"] = source

    checks: list[dict] = []
    seen: set = set()
    for source, doc in [*layers, (CLI, {"checks": [{"run": c} for c in cli_checks]})]:
        for check in doc.get("checks", []):
            key = _check_key(check)
            if key not in seen:
                seen.add(key)
                prov[f"checks[{len(checks)}]"] = source
                checks.append(check)

    if roles:
        config["roles"] = roles
    if flows:
        config["flows"] = flows
    if harnesses:
        config["harnesses"] = harnesses
    if sessions:
        config["sessions"] = sessions
    if consults:
        config["consults"] = consults
    if checks:
        config["checks"] = checks

    # the selected role: CLI, else the nearest default unless legacy flags bypass it
    role, role_source = None, None
    if cli_role:
        role, role_source = cli_role, CLI
    elif not (cli_runtime or cli_model):
        for source, doc in reversed(layers):
            if doc.get("defaults", {}).get("role"):
                role, role_source = doc["defaults"]["role"], source
                break
    if role is not None:
        if role not in roles:
            _fail("defaults.role" if role_source != CLI else "--role",
                  f"names no role ({', '.join(sorted(roles)) or 'none defined'})")
        config["defaults"] = {"role": role}
        prov["defaults.role"] = role_source
        spec = roles[role]
        if cli_runtime and cli_runtime != spec["harness"]:
            _fail("--runtime", f"{cli_runtime} contradicts role {role} ({spec['harness']})",
                  code="config.conflict")
        if cli_model and spec.get("model_id") and cli_model != spec["model_id"]:
            _fail("--model", f"{cli_model} contradicts role {role} ({spec['model_id']})",
                  code="config.conflict")
    _check_resolved(config, prov, files or {})
    return Resolved(config=config, provenance=prov, role=role)


def _check_resolved(config: dict, prov: dict[str, str], files: dict[str, str]) -> None:
    def fail(path: str, owner: str, message: str) -> None:
        # Name the file that defined the failing value, as load_file does.
        file = files.get(prov.get(owner, ""))
        _fail(path, f"{file}: {message}" if file else message)

    roles = config.get("roles", {})
    harnesses = config.get("harnesses", {})
    for name, role in roles.items():
        candidates = [(role, f"roles.{name}")] + [
            (candidate, f"roles.{name}.fallback[{index}]")
            for index, candidate in enumerate(role.get("fallback", []))]
        for candidate, path in candidates:
            allowed = harnesses.get(candidate["harness"], {}).get("allowed_models")
            if (allowed is not None and candidate.get("model_id")
                    and candidate["model_id"] not in allowed):
                fail(f"{path}.model_id", f"roles.{name}",
                     f"{candidate['model_id']} is not in "
                     f"harnesses.{candidate['harness']}.allowed_models")
        for consulted in role.get("consult", []):
            if consulted not in roles:
                fail(f"roles.{name}.consult", f"roles.{name}", f"names no role {consulted!r}")
    for name, flow in config.get("flows", {}).items():
        for i, step in enumerate(flow["steps"]):
            for ref in [step.get("role"), *step.get("parallel", []),
                        step.get("review", {}).get("by")]:
                if ref and ref not in roles:
                    fail(f"flows.{name}.steps[{i}]", f"flows.{name}", f"names no role {ref!r}")


def load_effective(workspace, *, cli_role=None, cli_runtime=None, cli_model=None,
                   cli_checks=()) -> Resolved | None:
    """Both files layered with the CLI request; ``None`` when neither file exists.

    The project file is read once, without following symlinks; values that
    need trust are withheld unless those exact bytes are trusted (C.2).
    """
    from garuda.config.project_trust import load_project

    user = load_file(user_path())
    loaded = load_project(workspace)
    project = loaded.doc
    if user is None and project is None and not cli_role and not cli_checks:
        return None
    from garuda.agents.authority import project_ceiling
    from garuda.config.agent_home import _load_global_settings

    settings = _load_global_settings()
    try:
        ceiling = project_ceiling(settings)
    except ConfigError as exc:
        raise GarudaConfigError("config.invalid", "agents.project_ceiling", str(exc)) from exc
    capacity = settings.get("capacity")
    resolved = resolve(user, project, cli_role=cli_role, cli_runtime=cli_runtime,
                       cli_model=cli_model, cli_checks=list(cli_checks),
                       legacy_capacity=capacity if isinstance(capacity, dict) else None,
                       project_ceiling=ceiling,
                       files={USER: str(user_path()),
                              **({PROJECT: str(loaded.file.path)} if loaded.file else {})})
    resolved.withheld = loaded.withheld
    if not loaded.trusted:
        resolved.external_project_models = frozenset(
            name for name, role in (project or {}).get("roles", {}).items()
            if "model_id" in role
        )
    return resolved


# --- migration ---------------------------------------------------------------------


def migrate(settings: dict, existing: dict | None) -> dict:
    """The user ``garuda.yaml`` that carries what ``settings.yaml`` already says.

    Trusted runtimes become ``harnesses`` and ``capacity`` becomes their
    ``max_parallel``. Nothing in ``settings.yaml`` is removed — both keep
    working — and a value that disagrees with ``existing`` is a conflict.
    Applying the result again changes nothing.
    """
    doc = copy.deepcopy(existing) if existing else {"version": VERSION}
    harnesses = doc.setdefault("harnesses", {})
    for i, manifest in enumerate(settings.get("runtimes") or []):
        if isinstance(manifest, dict) and isinstance(manifest.get("runtime_id"), str):
            harnesses.setdefault(_name(manifest["runtime_id"], f"settings.runtimes[{i}]"), {})
    capacity = settings.get("capacity") or {}
    if not isinstance(capacity, dict):
        _fail("settings.capacity", "must be a mapping")
    for name, limit in capacity.items():
        spec = harnesses.setdefault(_name(name, f"settings.capacity.{name}"), {})
        limit = _int(limit, f"settings.capacity.{name}", 1, 64)
        if "max_parallel" in spec and spec["max_parallel"] != limit:
            _fail(f"harnesses.{name}.max_parallel",
                  f"is {spec['max_parallel']} but settings.yaml capacity.{name} is {limit}",
                  code="config.conflict")
        spec["max_parallel"] = limit
    if not harnesses:
        doc.pop("harnesses")
    return parse(doc)


def semantic_diff(before: dict | None, after: dict) -> list[str]:
    """Leaf-level changes, as ``+ path: value`` / ``~ path: old -> new``."""
    def leaves(doc, prefix=""):
        if isinstance(doc, dict) and doc:
            for key, value in doc.items():
                yield from leaves(value, f"{prefix}.{key}" if prefix else key)
        else:
            yield prefix, doc

    old = dict(leaves(before or {}))
    new = dict(leaves(after))
    lines = [f"+ {k}: {v}" for k, v in new.items() if k not in old]
    lines += [f"~ {k}: {old[k]} -> {v}" for k, v in new.items() if k in old and old[k] != v]
    lines += [f"- {k}: {v}" for k, v in old.items() if k not in new]
    return lines
