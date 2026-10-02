"""One resolver for agent definitions (plan task H.1, #159).

Every format and entry point resolves an agent through :func:`resolve_agent`:

1. **Locate.** A bare name resolves project (``.agent/agents/`` then
   ``.garuda/agents/``), then user (``<global home>/agents/``), then packaged.
   ``garuda/<name>`` always means packaged; ``user/<name>`` and
   ``project/<name>`` are explicit. Names are bounded identifiers: traversal,
   separators, absolute paths and unknown prefixes refuse.
2. **Read** each file once, bounded, without following a symlink, and only
   if it stays inside its location.
3. **Parse** safe YAML or ``agent.md`` front matter: duplicate keys, unsafe
   tags, too many aliases, too deep or too large refuse, as does an unknown
   future ``version``. A legacy profile (no ``version``) is translated key by
   key into version 1 (unknown non-security keys warn; invalid security
   values refuse). In ``agent.md`` the body is the instruction text; a body and
   ``instructions.text`` together refuse.
4. **Extend.** ``extends`` names one agent, up to depth four; a cycle — a
   bare self-reference included — refuses (``agent.extends_cycle``). Leaves
   merge key by key (maps merge, scalars and plain lists replace),
   ``instructions`` append or replace, ``tools.add``/``remove`` apply to the
   parent's resolved list after a non-``inherit`` preset resets it.
5. **Record** where every resolved value came from. Authority is applied after
   the merge, from that provenance: a project file never inherits its way out
   of the project ceiling.

Resolution is pure: it reads definitions and nothing else. Activation
(:func:`activate`) refuses recognized-but-unsupported fields.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from garuda.agents import spec

logger = logging.getLogger(__name__)

MAX_BYTES = 256 * 1024
MAX_ALIASES = 64
MAX_DEPTH = 16
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
NAMESPACES = ("garuda", "user", "project")
PACKAGED, USER, PROJECT, INLINE = "packaged", "user", "project", "inline"
_ALIASES = {"model_bindings": "model_binding"}


@dataclass(frozen=True)
class Source:
    authority: str
    path: Path | None
    digest: str
    qualified: str  # garuda/build, user/x, project/x, or inline
    root: Path | None = None  # what its file references may not leave

    @property
    def identity(self) -> tuple:
        return (str(self.path), self.digest)


@dataclass
class ResolvedAgent:
    name: str
    description: str
    leaves: dict[str, Any]
    provenance: dict[str, Source]
    instructions: str | None
    tools: list[str] | None
    chain: list[Source]
    declared: set[str] = field(default_factory=set)  # legacy profile fields set
    warnings: list[str] = field(default_factory=list)
    versioned: bool = False  # the requested file itself is version 1
    removed: set[str] = field(default_factory=set)  # tools any level removed

    @property
    def source(self) -> Source:
        return self.chain[-1]

    def to_profile(self):
        """The :class:`AgentProfile` projection existing callers read."""
        from garuda.agents.loader import AgentProfile

        values: dict[str, Any] = {}
        for path, value in self.leaves.items():
            target = spec.FIELDS.get(path)
            if target is not None and target.profile and target.supported:
                values[target.profile] = value
        values["name"] = self.name
        values["description"] = self.description
        values["system_prompt"] = self.instructions
        values["tools"] = self.tools
        values = {k: v for k, v in values.items() if v is not None or k in (
            "system_prompt", "tools")}
        if values.get("system_prompt") is None:
            values.pop("system_prompt")
        if values.get("tools") is None:
            values.pop("tools")
        if self.versioned and "memory.user" not in self.leaves:
            values["memory_user"] = True  # version 1 default: your AGENTS.md when it exists
        if self.versioned and "tools.subagents" not in self.leaves:
            values["subagents"] = list(DEFAULT_SUBAGENTS)  # read-only agents only
        values["tools_removed"] = sorted(self.removed) or None
        return AgentProfile(declared_fields=set(self.declared), source_path=self.source.path,
                            **values)


# --- locating and reading -----------------------------------------------------------


def _defaults_dir() -> Path:
    from garuda.agents.loader import _defaults_dir as packaged

    return packaged()


def user_agents_dir() -> Path:
    from garuda.config.agent_home import global_settings_path

    return global_settings_path().expanduser().parent / "agents"


def _split(ref: str) -> tuple[str | None, str]:
    if not isinstance(ref, str) or not ref:
        raise spec.AgentSpecError("agent.invalid_name", "", f"{ref!r} is not an agent name")
    if os.path.isabs(ref) or "\\" in ref or ".." in ref.split("/"):
        raise spec.AgentSpecError("agent.invalid_name", "", f"{ref!r} is not an agent name")
    parts = ref.split("/")
    if len(parts) == 1:
        namespace, name = None, parts[0]
    elif len(parts) == 2 and parts[0] in NAMESPACES:
        namespace, name = parts
    else:
        raise spec.AgentSpecError("agent.invalid_name", "",
                                  f"{ref!r}: use a name, or garuda/, user/ or project/<name>")
    if not _NAME.match(name):
        raise spec.AgentSpecError("agent.invalid_name", "", f"{name!r} is not an agent name")
    return namespace, name


def _locations(namespace: str | None, project_dirs: list[Path]) -> list[tuple[str, Path]]:
    order = [(PROJECT, d) for d in project_dirs] + [(USER, user_agents_dir()),
                                                    (PACKAGED, _defaults_dir())]
    wanted = {"garuda": PACKAGED, "user": USER, "project": PROJECT}.get(namespace)
    return [(a, d) for a, d in order if wanted is None or a == wanted]


def _candidates(directory: Path, name: str) -> list[Path]:
    return [directory / f"{name}.yaml", directory / f"{name}.yml", directory / f"{name}.md",
            directory / name / "agent.md"]


def _read(path: Path, root: Path) -> bytes:
    """Read once, bounded, no-follow, and only inside ``root``."""
    try:
        real = path.resolve(strict=True)
    except OSError as exc:
        raise FileNotFoundError(path) from exc
    root_real = root.resolve()
    if real != root_real and root_real not in real.parents:
        raise spec.AgentSpecError("agent.path_escapes", "", f"{path} leaves {root}")
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as exc:
        raise spec.AgentSpecError("agent.path_escapes", "",
                                  f"{path} is a symlink or unreadable: {exc}") from exc
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise spec.AgentSpecError("agent.invalid_value", "", f"{path} is not a file")
        data = os.read(fd, MAX_BYTES + 1)
    finally:
        os.close(fd)
    if len(data) > MAX_BYTES:
        raise spec.AgentSpecError("agent.too_large", "", f"{path} is over {MAX_BYTES} bytes")
    return data


def locate(ref: str, project_dirs: list[Path]) -> tuple[Source, bytes]:
    namespace, name = _split(ref)
    for authority, directory in _locations(namespace, project_dirs):
        for path in _candidates(directory, name):
            if not (path.exists() or path.is_symlink()):
                continue
            data = _read(path, directory)
            qualified = {PACKAGED: "garuda", USER: "user", PROJECT: "project"}[authority]
            # A project definition's references stay in its repository; a user's
            # in the user's Garuda home; a packaged one's in the package.
            root = {PROJECT: directory.parent.parent, USER: directory.parent,
                    PACKAGED: directory}[authority]
            return Source(authority, path, hashlib.sha256(data).hexdigest(),
                          f"{qualified}/{name}", root), data
    raise FileNotFoundError(f"Agent profile not found: {ref}")


# --- parsing --------------------------------------------------------------------------


def _depth(value, level=0) -> int:
    if isinstance(value, dict):
        return max([level] + [_depth(v, level + 1) for v in value.values()])
    if isinstance(value, list):
        return max([level] + [_depth(v, level + 1) for v in value])
    return level


def _yaml(text: str, where: str):
    import yaml

    from garuda.agents.frontmatter import load_yaml_unique

    try:
        aliases = sum(isinstance(e, yaml.AliasEvent) for e in yaml.parse(text))
        if aliases > MAX_ALIASES:
            raise spec.AgentSpecError("agent.invalid_yaml", "", f"{where}: too many aliases")
        data = load_yaml_unique(text)
    except yaml.YAMLError as exc:
        from garuda.agents.loader import _refuse

        _refuse(Path(where), f"is not valid YAML: {exc}")
    if _depth(data) > MAX_DEPTH:
        raise spec.AgentSpecError("agent.invalid_yaml", "", f"{where}: nested too deeply")
    return data


def parse_source(source: Source, data: bytes) -> tuple[dict, list[str], set[str], bool]:
    """``(version 1 document, warnings, declared legacy fields, was version 1)``."""
    text = data.decode("utf-8")
    where = str(source.path)
    body = None
    if source.path is not None and source.path.suffix == ".md":
        import yaml

        from garuda.agents.frontmatter import parse_frontmatter

        try:
            meta, body = parse_frontmatter(text, unique_keys=True)
        except yaml.YAMLError as exc:
            from garuda.agents.loader import _refuse

            _refuse(source.path, f"has invalid front matter: {exc}")
        if _depth(meta) > MAX_DEPTH:
            raise spec.AgentSpecError("agent.invalid_yaml", "", f"{where}: nested too deeply")
        body = body or None
    else:
        meta = _yaml(text, where)
    if meta is None:
        meta = {}
    if not isinstance(meta, dict):
        from garuda.agents.loader import _refuse

        _refuse(source.path, "must be a mapping of profile fields")
    if "version" in meta:
        doc = dict(meta)
        if body is not None:
            instructions = dict(doc.get("instructions") or {})
            if "text" in instructions:
                raise spec.AgentSpecError(
                    "agent.ambiguous_instructions", "instructions.text",
                    "both the Markdown body and instructions.text are set", source=where)
            instructions["text"] = body
            doc["instructions"] = instructions
        spec.validate(doc, source=where)
        declared = _declared(spec.flatten({k: v for k, v in doc.items() if k != "version"}))
        return doc, [], declared, True
    return (*translate_legacy(meta, body, source=source), False)


def translate_legacy(data: dict, body: str | None, *, source: Source | None = None
                     ) -> tuple[dict, list[str], set[str]]:
    """A legacy profile mapping as a version 1 document (see ``spec.LEGACY``)."""
    import difflib

    from garuda.agents.loader import _validate_security_fields

    path = source.path if source else None
    _validate_security_fields(data, path)
    warnings: list[str] = []
    leaves: dict[str, Any] = {}
    declared: set[str] = set()
    known = set(spec.LEGACY) | set(spec.LEGACY_SPECIAL) | set(_ALIASES)
    for raw_key, value in data.items():
        key = _ALIASES.get(raw_key, raw_key)
        if key in ("declared_fields", "source_path", "spec_version"):
            continue
        if raw_key != key and key in data:
            continue  # the canonical spelling wins over the alias
        if key not in known:
            close = difflib.get_close_matches(str(raw_key), sorted(known), n=1)
            hint = f"; did you mean {close[0]!r}?" if close else ""
            message = f"Profile {path or '(inline)'}: unknown key {raw_key!r} is ignored{hint}"
            logger.warning(message)
            warnings.append(message)
            continue
        declared.add(key)
        if key in ("tools", "skills", "skills_dirs", "mcp_servers") and isinstance(value, str):
            value = [value]
        if key == "system_prompt":
            if value:
                leaves["instructions.mode"] = "replace"
                leaves["instructions.text"] = value
            continue
        if key == "tools":
            if value is not None:
                leaves["tools.preset"] = "none"
                leaves["tools.add"] = list(value)
            continue
        leaves[spec.LEGACY[key]] = value
    if body:
        leaves["instructions.mode"] = "replace"
        leaves["instructions.text"] = body
        declared.add("system_prompt")
    doc = {"version": spec.VERSION, **spec.nest(leaves)}
    return doc, warnings, declared


def _declared(leaves: dict[str, Any]) -> set[str]:
    out = set()
    for path in leaves:
        if path.startswith(("memory.", "tools.subagents", "tools.options")):
            continue  # memory sources and tool settings do not steer mode presets
        target = spec.FIELDS.get(path)
        if target is not None and target.profile:
            out.add(target.profile)
        elif path.startswith("instructions.text"):
            out.add("system_prompt")
        elif path.startswith("tools.") and path.split(".")[1] in ("preset", "add", "remove"):
            out.add("tools")
    return out


# --- resolving --------------------------------------------------------------------------


def _merge(current: dict[str, Any], prov: dict[str, Source], leaves: dict[str, Any],
           source: Source) -> None:
    for path, value in leaves.items():
        target = spec.FIELDS.get(path)
        if (target is not None and target.kind in ("rules", "map") and isinstance(value, dict)
                and isinstance(current.get(path), dict)):
            current[path] = {**current[path], **value}
        else:
            current[path] = value
        prov[path] = source


def builtin_tools() -> list[str]:
    from garuda.tools.registry import list_tool_names

    return sorted(list_tool_names())


def read_only_tools() -> list[str]:
    """Built-ins that neither write, run commands nor use the network — read from
    each tool's declared effect, never a hand-kept list — plus task_complete."""
    from garuda.tools.protocol import ToolEffect, tool_effect
    from garuda.tools.registry import get_tool

    names = [n for n in builtin_tools() if tool_effect(get_tool(n)) is ToolEffect.READ_ONLY]
    return sorted({*names, "task_complete"})


def _apply_tools(base: list[str] | None, leaves: dict[str, Any], where: str) -> list[str] | None:
    preset = leaves.get("tools.preset", "inherit")
    add, remove = leaves.get("tools.add", []), leaves.get("tools.remove", [])
    if preset == "none":
        base = []
    elif preset == "all":
        base = builtin_tools()
    elif preset == "read-only":
        base = read_only_tools()
    if base is None and (add or remove):
        base = builtin_tools()  # an unrestricted list edited by name starts from every built-in
    if base is None:
        return None
    out = list(base)
    for name in add:
        if name not in out:
            out.append(name)
    return [name for name in out if name not in set(remove)]


DEFAULT_SUBAGENTS = ("explore", "plan", "reviewer")


def _check_options(options, where: str) -> None:
    """``tools.options``: every tool and option must be declared, every value valid."""
    from garuda.tools.registry import get_tool

    for tool_name, values in options.items():
        try:
            tool = get_tool(tool_name)
        except KeyError:
            tool = None
        schema = getattr(tool, "options_schema", None) if tool else None
        if not schema:
            raise spec.AgentSpecError("agent.unknown_tool_option", f"tools.options.{tool_name}",
                                      f"{tool_name!r} takes no options", source=where)
        if not isinstance(values, dict):
            raise spec.AgentSpecError("agent.invalid_value", f"tools.options.{tool_name}",
                                      "must be a mapping of option to value", source=where)
        for option, value in values.items():
            check = schema.get(option)
            if check is None:
                raise spec.AgentSpecError(
                    "agent.unknown_tool_option", f"tools.options.{tool_name}.{option}",
                    f"{tool_name} has no option {option!r} (it has {', '.join(sorted(schema))})",
                    source=where)
            problem = check(value)
            if problem:
                raise spec.AgentSpecError("agent.invalid_value",
                                          f"tools.options.{tool_name}.{option}", problem,
                                          source=where)


def _check_tools(names, where: str) -> None:
    from garuda.tools.registry import list_tool_names

    known = set(list_tool_names())
    for name in names:
        if name not in known and not name.startswith("mcp__"):
            raise spec.AgentSpecError("agent.unknown_tool", "tools.add",
                                      f"{name!r} is not a tool", source=where)


def resolve_agent(ref: str, project_dirs=None, *, _stack: tuple = ()) -> ResolvedAgent:
    """Resolve ``ref`` (a name or qualified name) through every ``extends``."""

    dirs = [Path(d) for d in (project_dirs or [])]
    source, data = locate(ref, dirs)
    return resolve_source(source, data, dirs, _stack=_stack)


def resolve_source(source: Source, data: bytes, dirs: list[Path], *,
                   _stack: tuple = ()) -> ResolvedAgent:
    """Resolve one already-read definition file through its ``extends``."""
    from garuda.types import DEFAULT_SYSTEM_PROMPT

    if source.identity in {s.identity for s in _stack}:
        raise spec.AgentSpecError("agent.extends_cycle", "extends",
                                  " -> ".join(s.qualified for s in (*_stack, source)))
    doc, warnings, declared, is_v1 = parse_source(source, data)
    leaves = spec.flatten({k: v for k, v in doc.items() if k != "version"})
    if is_v1:
        _check_tools(leaves.get("tools.add", []), str(source.path))
    parent_ref = leaves.pop("extends", None)
    if parent_ref:
        if len(_stack) >= spec.MAX_EXTENDS_DEPTH:
            raise spec.AgentSpecError("agent.extends_too_deep", "extends",
                                      f"more than {spec.MAX_EXTENDS_DEPTH} levels of extends")
        namespace, parent_name = _split(parent_ref)
        if namespace is None and parent_name == Path(source.qualified).name:
            raise spec.AgentSpecError(
                "agent.extends_cycle", "extends",
                f"{source.qualified} extends itself; extend garuda/{parent_name} to change "
                "the packaged agent", source=str(source.path))
        parent = resolve_agent(parent_ref, dirs, _stack=(*_stack, source))
        merged, prov = dict(parent.leaves), dict(parent.provenance)
        instructions, tools = parent.instructions, parent.tools
        removed = set(parent.removed)
        declared = set(parent.declared) | declared
        warnings = parent.warnings + warnings
        chain = [*parent.chain, source]
    else:
        merged, prov, instructions, tools, chain = {}, {}, None, None, [source]
        removed = set()
    own = {k: v for k, v in leaves.items() if not k.startswith(("instructions.",))
           and k not in ("tools.preset", "tools.add", "tools.remove")}
    _merge(merged, prov, own, source)
    text = leaves.get("instructions.text")
    files = leaves.get("instructions.files") or []
    if files:
        parts = [text] if text else []
        for rel in files:
            parts.append(_instruction_file(source, rel))
        text = "\n\n".join(parts)
    if text is not None:
        mode = leaves.get("instructions.mode", "append" if is_v1 else "replace")
        instructions = text if mode == "replace" else (
            (instructions or DEFAULT_SYSTEM_PROMPT).rstrip() + "\n\n" + text)
        prov["instructions"] = source
    tool_leaves = {k: v for k, v in leaves.items() if k.startswith("tools.")
                   and k in ("tools.preset", "tools.add", "tools.remove")}
    if tool_leaves:
        tools = _apply_tools(tools, tool_leaves, str(source.path))
        prov["tools"] = source
        removed = (removed - set(tool_leaves.get("tools.add", []))) | set(
            tool_leaves.get("tools.remove", []))
    if "tools.options" in leaves:
        _check_options(leaves["tools.options"], str(source.path))
    for path in ("tools.preset", "tools.add", "tools.remove"):
        if path in leaves:
            merged[path] = leaves[path]  # kept for activation checks and display
    name = merged.get("name") if prov.get("name") is source else None
    stem = source.path.parent.name if source.path and source.path.name == "agent.md" else (
        source.path.stem if source.path else "inline")
    return ResolvedAgent(
        name=name or stem,
        description=merged.get("description", ""),
        leaves=merged, provenance=prov, instructions=instructions, tools=tools,
        chain=chain, declared=declared, warnings=warnings, versioned=is_v1,
        removed=removed,
    )


def _instruction_file(source: Source, rel: str) -> str:
    """An instruction file, relative to the declaring file and inside its root."""
    base = source.path.parent if source.path else Path.cwd()
    target = Path(rel)
    if target.is_absolute() or ".." in target.parts:
        raise spec.AgentSpecError("agent.path_escapes", "instructions.files",
                                  f"{rel} must be a relative path inside the agent's root",
                                  source=str(source.path))
    path = base / target
    if not path.exists():
        raise spec.AgentSpecError("agent.instruction_file_missing", "instructions.files",
                                  f"{rel} does not exist", source=str(source.path))
    return _read(path, source.root or base).decode("utf-8")


def check_references(profile, workspace, *, mcp_config_path: str | None = None) -> None:
    """Version 1 refusals that need the workspace: unknown skills, subagents and MCP
    servers.

    Reads configuration only; nothing is started.
    """
    if getattr(profile, "spec_version", None) != spec.VERSION:
        return
    if profile.skills:
        from garuda.skills.sources import select

        unknown = select(profile, workspace).unknown
        if unknown:
            raise spec.AgentSpecError("agent.unknown_skill", "skills.include",
                                      f"no skill named {unknown[0]!r} in the selected sources",
                                      source=str(profile.source_path))
    if profile.subagents:
        from garuda.agents.loader import load_profile
        from garuda.config.agent_home import resolve_agents_dirs
        from garuda.model.config import ConfigError

        dirs = resolve_agents_dirs(str(workspace), None)
        for name in profile.subagents:
            try:
                load_profile(name, extra_dir=dirs)
            except (ConfigError, OSError) as exc:
                raise spec.AgentSpecError("agent.unknown_subagent", "tools.subagents",
                                          f"subagent {name!r} does not load: {exc}",
                                          source=str(profile.source_path)) from exc
    if profile.mcp_servers:
        from garuda.mcp.config import load_mcp_config, resolve_mcp_config_paths

        names = set()
        for path in resolve_mcp_config_paths(workspace, mcp_config_path or
                                             profile.mcp_config_path):
            try:
                names |= {server.name for server in load_mcp_config(path)}
            except Exception:
                continue
        for name in profile.mcp_servers:
            if name not in names:
                raise spec.AgentSpecError("agent.unknown_mcp_server", "tools.mcp",
                                          f"no MCP server named {name!r} is configured",
                                          source=str(profile.source_path))


def activate(agent: ResolvedAgent):
    """Refuse recognized-but-unsupported fields, then project to a profile."""
    pending = spec.unsupported({**agent.leaves})
    if pending:
        raise spec.AgentSpecError(
            "agent.unsupported_field", pending[0].split(" ")[0],
            f"recognized but not supported yet: {', '.join(pending)}",
            source=str(agent.source.path))
    if agent.versioned:
        from garuda.agents.compile import check

        check(agent)
    profile = agent.to_profile()
    profile.spec_version = spec.VERSION if agent.versioned else None
    return profile
