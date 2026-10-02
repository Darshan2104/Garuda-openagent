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

from garuda.model.config import ConfigError

VERSION = 1
MAX_STEPS = 32
MAX_PARALLEL_REVIEWERS = 16
MAX_RETRIES = 10
MAX_CONSULTS = 100
PERMISSIONS = ("readonly", "smart", "auto", "yolo")  # most to least restrictive
EFFORTS = ("minimal", "low", "medium", "high")
WRITE_POLICIES = ("edits", "no-edits")
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


def _harness(value, path) -> dict:
    data = _mapping(value or {}, path, ("allowed_models", "max_parallel"))
    out: dict[str, Any] = {}
    if "allowed_models" in data:
        out["allowed_models"] = _strs(data["allowed_models"], f"{path}.allowed_models")
    if "max_parallel" in data:
        out["max_parallel"] = _int(data["max_parallel"], f"{path}.max_parallel", 1, 64)
    return out


def _candidate(value, path) -> dict:
    data = _mapping(value, path, ("harness", "model_id"))
    out = {"harness": _name(data.get("harness"), f"{path}.harness")}
    if "model_id" in data:
        out["model_id"] = _str(data["model_id"], f"{path}.model_id")
    return out


def _role(value, path) -> dict:
    data = _mapping(value, path, ("harness", "model_id", "effort", "permissions", "write_policy",
                                  "profile", "fallback", "consult", "description"))
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
    if "description" in data:
        out["description"] = _str(data["description"], f"{path}.description")
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


def _step(value, path) -> dict:
    if isinstance(value, dict) and "flow" in value:
        _fail(f"{path}.flow", "a step cannot be a flow (nested flows are not supported)")
    data = _mapping(value, path, ("id", "role", "parallel", "write_policy", "inputs", "outputs",
                                  "retries", "review"))
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
    for key in ("inputs", "outputs"):
        if key in data:
            out[key] = _artifacts(data[key], f"{path}.{key}")
    if "retries" in data:
        out["retries"] = _int(data["retries"], f"{path}.retries", 0, MAX_RETRIES)
    if "review" in data:
        review = _mapping(data["review"], f"{path}.review", ("by", "max_rounds"))
        out["review"] = {"by": _name(review.get("by"), f"{path}.review.by")}
        if "max_rounds" in review:
            out["review"]["max_rounds"] = _int(review["max_rounds"], f"{path}.review.max_rounds",
                                               1, MAX_RETRIES)
    return out


def _flow(value, path) -> dict:
    data = _mapping(value, path, ("steps", "description"))
    steps = data.get("steps")
    if not isinstance(steps, list) or not steps:
        _fail(f"{path}.steps", "must be a non-empty list")
    if len(steps) > MAX_STEPS:
        _fail(f"{path}.steps", f"at most {MAX_STEPS} steps")
    out: dict[str, Any] = {"steps": [_step(s, f"{path}.steps[{i}]") for i, s in enumerate(steps)]}
    ids = [s["id"] for s in out["steps"] if "id" in s]
    if len(set(ids)) != len(ids):
        _fail(f"{path}.steps", "step ids must be unique")
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
                            ("max_per_session", "timeout_sec", "max_answer_chars"))
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
    for name, flow in doc.get("flows", {}).items():
        for i, step in enumerate(flow["steps"]):
            review = step.get("review")
            if review and "parallel" not in step and review["by"] == step.get("role"):
                _fail(f"flows.{name}.steps[{i}].review.by", "a step cannot review itself")
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
    from garuda.core.sessions import project_root

    return Path(project_root(str(Path(workspace).resolve()))) / "garuda.yaml"


def load_file(path: Path) -> dict | None:
    try:
        text = path.read_text(encoding="utf-8")
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


def _narrow_role(name: str, user: dict | None, project: dict) -> dict:
    path = f"roles.{name}"
    if user is None:
        for key in ("fallback", "consult"):
            if project.get(key):
                _fail(f"{path}.{key}", f"{key} grants are user-only; a project cannot add them",
                      code="config.project_widening")
        return project
    role = dict(project)
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
) -> Resolved:
    """Layer package < user < project < CLI. See the module docstring."""
    layers = [(PACKAGE, package or {}), (USER, user or {}), (PROJECT, project or {})]
    config: dict[str, Any] = {"version": VERSION}
    prov: dict[str, str] = {}

    # roles and flows: whole-definition replacement, with project narrowing
    roles: dict[str, dict] = {}
    for source, doc in layers:
        for name, role in doc.get("roles", {}).items():
            if source == PROJECT:
                role = _narrow_role(name, roles.get(name), role)
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
    _check_resolved(config)
    return Resolved(config=config, provenance=prov, role=role)


def _check_resolved(config: dict) -> None:
    roles = config.get("roles", {})
    harnesses = config.get("harnesses", {})
    for name, role in roles.items():
        allowed = harnesses.get(role["harness"], {}).get("allowed_models")
        if allowed is not None and role.get("model_id") and role["model_id"] not in allowed:
            _fail(f"roles.{name}.model_id",
                  f"{role['model_id']} is not in harnesses.{role['harness']}.allowed_models")
        for consulted in role.get("consult", []):
            if consulted not in roles:
                _fail(f"roles.{name}.consult", f"names no role {consulted!r}")
    for name, flow in config.get("flows", {}).items():
        for i, step in enumerate(flow["steps"]):
            for ref in [step.get("role"), *step.get("parallel", []),
                        step.get("review", {}).get("by")]:
                if ref and ref not in roles:
                    _fail(f"flows.{name}.steps[{i}]", f"names no role {ref!r}")


def load_effective(workspace, *, cli_role=None, cli_runtime=None, cli_model=None,
                   cli_checks=()) -> Resolved | None:
    """Both files layered with the CLI request; ``None`` when neither file exists."""
    user = load_file(user_path())
    project = load_file(project_path(workspace))
    if user is None and project is None and not cli_role:
        return None
    from garuda.config.agent_home import _load_global_settings

    capacity = _load_global_settings().get("capacity")
    return resolve(user, project, cli_role=cli_role, cli_runtime=cli_runtime,
                   cli_model=cli_model, cli_checks=list(cli_checks),
                   legacy_capacity=capacity if isinstance(capacity, dict) else None)


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
