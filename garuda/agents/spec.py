"""The version 1 agent definition (plan task H.1, #159).

One field table drives everything: the strict validator, the legacy
translation (each legacy profile key maps to one version 1 path), and the
projection back to :class:`~garuda.agents.loader.AgentProfile` for callers that
still read profiles.

Every field is optional. Omission inherits; an explicit ``null`` is allowed
only for nullable fields; an empty selection disables the capability and
never means "all". A field the schema **recognizes** but whose owner PR has
not shipped refuses activation (``agent.unsupported_field``): recognizing a
field is not supporting it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from garuda.model.config import ConfigError

VERSION = 1
MAX_EXTENDS_DEPTH = 4
PERMISSIONS = ("readonly", "smart", "auto", "yolo")
TOOL_RULES = ("allow", "deny", "ask")
EFFORTS = ("minimal", "low", "medium", "high")


class AgentSpecError(ConfigError):
    """A refused definition. ``code`` is stable; ``path`` names the value."""

    def __init__(self, code: str, path: str, message: str, *, source: str = ""):
        where = f"{source}: " if source else ""
        super().__init__(f"{code}: {where}{path or '(file)'}: {message}")
        self.code = code
        self.path = path


@dataclass(frozen=True)
class Field:
    kind: str  # str | int | bool | choice | strs | rules | map | any
    profile: str | None = None  # the AgentProfile field it projects to
    supported: bool = True
    nullable: bool = False
    choices: tuple = ()
    owner: str = ""  # the plan task that ships an unsupported field


def _f(kind, profile=None, **kw) -> Field:
    return Field(kind, profile, **kw)


def _later(kind, owner, **kw) -> Field:
    return Field(kind, None, supported=False, owner=owner, **kw)


#: Every version 1 path. ``profile`` names the legacy field it maps to.
FIELDS: dict[str, Field] = {
    "name": _f("str", "name"),
    "description": _f("str", "description"),
    "extends": _f("str", nullable=True),
    # model
    "model.binding": _f("str", "model_binding", nullable=True),
    "model.effort": _f("choice", "reasoning_effort", nullable=True, choices=EFFORTS),
    "model.thinking_budget_tokens": _f("int", "thinking_budget_tokens", nullable=True),
    "model.max_output_tokens": _f("int", "max_tokens", nullable=True),
    "model.collection": _f("map", "collection", nullable=True),
    # instructions
    "instructions.mode": _f("choice", choices=("append", "replace")),
    "instructions.text": _f("str", nullable=True),
    "instructions.files": _f("strs"),
    # memory
    "memory.user": _f("bool", "memory_user"),
    "memory.project": _f("strs", "memory_project"),
    "memory.project_mode": _f("choice", "memory_project_mode", choices=("first", "all")),
    "memory.max_chars": _f("int", "memory_max_chars"),
    "memory.max_total_chars": _f("int", "memory_max_total_chars"),
    "memory.context_pack": _f("bool", "memory_context_pack"),
    "memory.notes": _later("choice", "H.9", choices=("off", "propose")),
    # skills
    "skills.include": _f("strs", "skills", nullable=True),
    "skills.dirs": _f("strs", "skills_dirs", nullable=True),
    "skills.from": _f("strs", "skills_from", nullable=True),
    "skills.exclude": _f("strs", "skills_exclude"),
    "skills.load": _f("choice", "skills_load", choices=("index", "full")),
    # tools
    "tools.preset": _f("choice", choices=("inherit", "none", "all", "read-only")),
    "tools.add": _f("strs"),
    "tools.remove": _f("strs"),
    "tools.mcp": _f("strs", "mcp_servers", nullable=True),
    "tools.mcp_config": _f("str", "mcp_config_path", nullable=True),
    "tools.tmux": _f("bool", "enable_tmux"),
    "tools.marker_polling": _f("bool", "marker_polling"),
    "tools.options": _f("map", "tool_options"),
    "tools.subagents": _f("strs", "subagents", nullable=True),
    "tools.subagent_only": _f("bool", "subagent"),
    # permissions
    "permissions.mode": _f("choice", "permission_mode", choices=PERMISSIONS),
    "permissions.rules.tools": _f("rules", "tool_rules", nullable=True),
    "permissions.rules.paths": _f("rules", "path_rules", nullable=True),
    "permissions.rules.bash": _f("rules", "bash_rules", nullable=True),
    "write_policy": _later("choice", "H.10", nullable=True, choices=("no-edits",)),
    # hooks
    "hooks.replace": _later("bool", "agent hooks"),
    "hooks.before_tool": _later("any", "agent hooks"),
    "hooks.after_tool": _later("any", "agent hooks"),
    "hooks.session_start": _later("any", "agent hooks"),
    "hooks.session_end": _later("any", "agent hooks"),
    # context
    "context.max_tokens": _f("int", "max_context_tokens"),
    "context.summarize_after_tokens": _f("int", "proactive_summarize_threshold"),
    "context.max_tool_output_bytes": _f("int", "max_output_bytes"),
    "context.min_tool_output_bytes": _f("int", "min_output_bytes"),
    "context.reserved_output_tokens": _f("int", "reserved_output_tokens"),
    "context.safety_margin_tokens": _f("int", "context_safety_margin_tokens"),
    "context.request_preflight": _f("bool", "enable_request_preflight"),
    "context.adaptive_output": _f("bool", "enable_adaptive_output"),
    "context.working_state_card": _f("bool", "enable_working_state_card"),
    "context.three_step_summary": _f("bool", "enable_three_step_summary"),
    "context.condenser": _f("choice", "condenser",
                            choices=("microcompact", "recent_window", "summarizing")),
    # limits
    "limits.max_turns": _f("int", "max_turns"),
    "limits.deadline_sec": _f("number", "deadline_sec", nullable=True),
    # completion
    "completion.mode": _f("str", "mode"),
    "completion.acceptance_contract": _f("bool", "enable_acceptance_contract", nullable=True),
    "completion.verifier": _f("bool", "enable_verifier"),
    # workspace
    "workspace.kind": _f("choice", "workspace_kind",
                         choices=("local", "sandbox", "tmux", "docker", "remote")),
    "workspace.docker.image": _f("str", "docker_image"),
    "workspace.docker.network": _f("bool", "docker_network"),
    "workspace.docker.memory": _f("str", "docker_memory"),
    "workspace.docker.cpus": _f("number", "docker_cpus"),
    # output
    "output.schema": _later("str", "H.12b", nullable=True),
}

#: Legacy profile key -> version 1 path. ``system_prompt`` and ``tools`` are special.
LEGACY = {f.profile: path for path, f in FIELDS.items() if f.profile}
LEGACY_SPECIAL = ("system_prompt", "tools")
SECTIONS = sorted({p.split(".")[0] for p in FIELDS})


def flatten(doc: dict, prefix: str = "") -> dict[str, Any]:
    """``{dotted path: value}`` for every leaf a document sets."""
    out: dict[str, Any] = {}
    for key, value in doc.items():
        path = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(value, dict) and path not in FIELDS:
            nested = flatten(value, path)
            if nested:
                out.update(nested)
            elif not any(f.startswith(path + ".") for f in FIELDS):
                out[path] = value  # an empty map that is not a section: still unknown
        else:
            out[path] = value
    return out


def nest(leaves: dict[str, Any]) -> dict:
    out: dict = {}
    for path, value in leaves.items():
        node = out
        parts = path.split(".")
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value
    return out


def _check_value(path: str, field: Field, value: Any, source: str) -> None:
    def bad(why):
        raise AgentSpecError("agent.invalid_value", path, why, source=source)

    if value is None:
        if not field.nullable:
            bad("may not be null")
        return
    kind = field.kind
    if kind == "str" and not isinstance(value, str):
        bad("must be a string")
    if kind == "int" and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
        bad("must be a non-negative integer")
    if kind == "number" and (isinstance(value, bool) or not isinstance(value, (int, float))
                             or value != value or value in (float("inf"), float("-inf"))
                             or value <= 0):
        bad("must be a positive, finite number")
    if kind == "bool" and not isinstance(value, bool):
        bad("must be true or false")
    if kind == "choice" and value not in field.choices:
        bad(f"must be one of {', '.join(field.choices)}")
    if kind == "strs" and (not isinstance(value, list)
                          or not all(isinstance(v, str) and v for v in value)):
        bad("must be a list of strings")
    if kind == "map" and not isinstance(value, dict):
        bad("must be a mapping")
    if kind == "rules":
        _check_rules(path, value, source)


def _check_rules(path: str, value: Any, source: str) -> None:
    if not isinstance(value, dict):
        raise AgentSpecError("agent.invalid_value", path, "must be a mapping", source=source)
    if path.endswith(".tools"):
        for tool, rule in value.items():
            if rule not in TOOL_RULES:
                raise AgentSpecError("agent.invalid_value", f"{path}.{tool}",
                                     "must be allow, deny or ask", source=source)
        return
    keys = ("deny", "ask") if path.endswith(".paths") else ("deny", "ask", "allow_prefixes")
    for key, rules in value.items():
        if key not in keys:
            raise AgentSpecError("agent.invalid_value", f"{path}.{key}",
                                 f"is not a rule; use {', '.join(keys)}", source=source)
        if not isinstance(rules, list) or not all(isinstance(r, str) for r in rules):
            raise AgentSpecError("agent.invalid_value", f"{path}.{key}",
                                 "must be a list of strings", source=source)


def validate(doc: dict, *, source: str = "") -> dict[str, Any]:
    """Validate a version 1 document; returns its leaves (dotted path -> value)."""
    if not isinstance(doc, dict):
        raise AgentSpecError("agent.invalid_value", "", "must be a mapping", source=source)
    if doc.get("version") != VERSION:
        raise AgentSpecError("agent.unsupported_version", "version",
                             f"{doc.get('version')!r} is not a version this Garuda reads",
                             source=source)
    leaves = flatten({k: v for k, v in doc.items() if k != "version"})
    for path, value in leaves.items():
        field = FIELDS.get(path)
        if field is None:
            if path in ("authority",) or path.endswith(".authority"):
                raise AgentSpecError("agent.unknown_field", path,
                                     "authority comes from where a file lives", source=source)
            raise AgentSpecError("agent.unknown_field", path, "is not a version 1 field",
                                 source=source)
        _check_value(path, field, value, source)
    return leaves


def unsupported(leaves: dict[str, Any]) -> list[str]:
    """Recognized fields whose owner PR has not shipped."""
    out = []
    for path in leaves:
        field = FIELDS.get(path)
        if field is not None and not field.supported:
            out.append(f"{path} (arrives with {field.owner})")

    return out
