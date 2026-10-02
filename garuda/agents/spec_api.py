"""``AgentSpec``: one resolved agent definition, the same from every entry point
(plan task H.8, #166).

An :class:`AgentSpec` is the result of **pure resolution**: the version 1 or
legacy definition, its ``extends`` chain, instructions, tools and any output
schema, read and checked, with nothing started — no model, MCP server, hook or
project code. Activation (models, toolkit, environment) stays in
``agents.setup.prepare_agent_run``, which accepts a spec wherever it accepts a
name, so the CLI, chat, ``serve``, the web dashboard and the SDK resolve one
definition identically and show the same ``agent show`` output and prompt digest.

A spec is immutable in use: every accessor hands out a copy, so two concurrent
runs (two server jobs, two SDK agents) never share a mutable definition.

``narrow`` returns a **stricter** copy and nothing else. Anything that could
widen authority — a looser permission mode, more tools, a wider MCP, subagent,
domain or budget allowance, disabling a required check — refuses with
``agent.narrow_refused``; a field not listed here refuses too (fail closed).
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
from typing import Any

from garuda.agents import resolve, spec
from garuda.agents.authority import PERMISSION_ORDER

#: Numbers that may only go down (``None`` currently means "no limit").
_CAPS = {
    "limits.max_turns", "limits.deadline_sec", "model.max_output_tokens", "context.max_tokens",
    "context.max_tool_output_bytes", "memory.max_chars", "memory.max_total_chars",
    "workspace.docker.cpus",
}
#: Switches that may only be turned off: they grant a capability.
_ONLY_OFF = {"memory.user", "memory.context_pack"}
#: Switches that may only be turned on: they are checks.
_ONLY_ON = {"completion.verifier", "completion.acceptance_contract"}
#: Lists that may only shrink (``None`` currently means "everything").
_SUBSETS = {"tools.mcp", "tools.subagents", "skills.include", "skills.from", "memory.project"}
#: Lists that may only grow (they exclude something).
_ONLY_GROW = {"skills.exclude"}
_RULE_ORDER = {"allow": 0, "ask": 1, "deny": 2}


def _refused(path: str, message: str) -> spec.AgentSpecError:
    return spec.AgentSpecError("agent.narrow_refused", path, message)


def _digest(agent: resolve.ResolvedAgent) -> str:
    material = {
        "leaves": agent.leaves, "instructions": agent.instructions, "tools": agent.tools,
        "removed": sorted(agent.removed), "output_schema": agent.output_schema,
        "chain": [s.digest for s in agent.chain], "versioned": agent.versioned,
    }
    encoded = json.dumps(material, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class AgentSpec:
    """A resolved agent definition. Build one with :meth:`load`, :meth:`from_file`
    or :meth:`from_dict`; narrow it with :meth:`narrow`."""

    __slots__ = ("_agent", "_workspace", "_narrowed", "_digest")

    def __init__(self, agent: resolve.ResolvedAgent, workspace: str | None = None,
                 narrowed: tuple = ()):
        self._agent = copy.deepcopy(agent)
        self._workspace = workspace
        self._narrowed = tuple(narrowed)
        self._digest = _digest(self._agent)

    # --- construction ---------------------------------------------------------------

    @classmethod
    def load(cls, name: str, workspace: str | Path = ".", agents_dir=None) -> AgentSpec:
        """Resolve a named agent (``build``, ``garuda/explore``, ``project/x``)."""
        from garuda.config.agent_home import resolve_agents_dirs

        dirs = resolve_agents_dirs(str(workspace), agents_dir)
        listed = dirs if isinstance(dirs, (list, tuple)) else ([dirs] if dirs else [])
        return cls(resolve.resolve_agent(name, [Path(d) for d in listed]), str(workspace))

    @classmethod
    def from_file(cls, path: str | Path, workspace: str | Path = ".", agents_dir=None
                  ) -> AgentSpec:
        """Resolve a definition file. This selects a source; it grants no trust: a file
        inside the workspace is still project content under the project ceiling."""
        file = Path(path).expanduser()
        if not file.is_file():
            raise FileNotFoundError(f"Agent file not found: {path}")
        from garuda.config.agent_home import resolve_agents_dirs

        dirs = resolve_agents_dirs(str(workspace), agents_dir)
        listed = dirs if isinstance(dirs, (list, tuple)) else ([dirs] if dirs else [])
        data = resolve._read(file, file.parent)
        source = resolve.Source(resolve.INLINE, file.resolve(), hashlib.sha256(data).hexdigest(),
                                f"file/{file.stem}", file.resolve().parent)
        return cls(resolve.resolve_source(source, data, [Path(d) for d in listed]), str(workspace))

    @classmethod
    def from_dict(cls, document: dict, workspace: str | Path = ".", agents_dir=None,
                  *, name: str = "inline") -> AgentSpec:
        """Resolve an inline definition (SDK use). It names no file, so it cannot
        reference instruction or schema files; ``extends`` resolves normally."""
        import yaml

        if not isinstance(document, dict):
            raise spec.AgentSpecError("agent.invalid_value", "", "an inline agent must be a mapping")
        from garuda.config.agent_home import resolve_agents_dirs

        dirs = resolve_agents_dirs(str(workspace), agents_dir)
        listed = dirs if isinstance(dirs, (list, tuple)) else ([dirs] if dirs else [])
        document = {"version": spec.VERSION, **document}
        data = yaml.safe_dump(document, sort_keys=True).encode("utf-8")
        source = resolve.Source(resolve.INLINE, None, hashlib.sha256(data).hexdigest(),
                                f"inline/{name}", None)
        agent = resolve.resolve_source(source, data, [Path(d) for d in listed])
        agent.name = document.get("name") or name
        return cls(agent, str(workspace))

    # --- reading ---------------------------------------------------------------------

    @property
    def name(self) -> str:
        return self._agent.name

    @property
    def digest(self) -> str:
        """Identifies exactly this definition (and any narrowing) for the session."""
        return self._digest

    @property
    def narrowed(self) -> tuple:
        return self._narrowed

    @property
    def source_path(self) -> Path | None:
        return self._agent.source.path

    def profile(self):
        """A fresh, activated :class:`AgentProfile` (refuses an unsupported field)."""
        profile = resolve.activate(copy.deepcopy(self._agent))
        profile.spec_digest = self._digest
        return profile

    def resolved(self) -> resolve.ResolvedAgent:
        return copy.deepcopy(self._agent)

    def show(self, workspace=None, *, raw: bool = False) -> dict:
        """What ``garuda agent show NAME --json`` prints for this definition."""
        from garuda.agents import inspect

        return inspect.show_agent(self.resolved(), workspace or self._workspace or ".", raw=raw)

    def prompt(self, workspace=None, *, raw: bool = False) -> dict:
        from garuda.agents import inspect

        return inspect.prompt_agent(self.resolved(), workspace or self._workspace or ".", raw=raw)

    # --- narrowing ----------------------------------------------------------------------

    def narrow(self, **sections: dict) -> AgentSpec:
        """A stricter copy: ``spec.narrow(limits={"max_turns": 40}, tools={"remove": ["bash"]})``.

        Raises ``agent.narrow_refused`` for anything that is not at least as strict."""
        for section, value in sections.items():
            if section not in spec.SECTIONS or not isinstance(value, dict):
                raise _refused(section, "narrow takes a section name and a mapping")
        leaves = spec.flatten(sections)
        agent = self.resolved()
        current = agent.to_profile()
        applied = []
        for path, value in leaves.items():
            self._narrow_one(agent, current, path, value)
            applied.append((path, json.dumps(value, sort_keys=True, default=str)))
        narrowed = Source_narrowed(agent)
        for path in leaves:
            agent.provenance[path] = narrowed
        return AgentSpec(agent, self._workspace, (*self._narrowed, *applied))

    def _narrow_one(self, agent, current, path: str, value: Any) -> None:
        field = spec.FIELDS.get(path)
        have = getattr(current, field.profile, None) if field and field.profile else None
        if path == "permissions.mode":
            if PERMISSION_ORDER.index(value) > PERMISSION_ORDER.index(have or "yolo"):
                raise _refused(path, f"{value} is looser than {have}")
        elif path in _CAPS:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise _refused(path, "must be a number")
            if have is not None and value > have:
                raise _refused(path, f"{value} is more than the current {have}")
        elif path in _ONLY_OFF:
            if value and not have:
                raise _refused(path, "can only be turned off")
        elif path in _ONLY_ON:
            if not value and have:
                raise _refused(path, "a required check cannot be turned off")
        elif path in _SUBSETS:
            if have is not None and not set(value) <= set(have):
                raise _refused(path, f"may only keep entries of {sorted(have)}")
        elif path in _ONLY_GROW:
            if not set(have or []) <= set(value):
                raise _refused(path, "may only add exclusions")
        elif path.startswith("permissions.rules."):
            agent.leaves[path] = self._narrow_rules(path, have, value)
            return
        elif path in ("tools.remove",):
            agent.removed |= set(value)
            base = agent.tools if agent.tools is not None else resolve.builtin_tools()
            agent.tools = [t for t in base if t not in set(value)]
            agent.leaves[path] = sorted(set(agent.leaves.get(path, [])) | set(value))
            return
        elif path == "tools.preset":
            if value not in ("none", "read-only"):
                raise _refused(path, "only none and read-only narrow the tool list")
            base = agent.tools if agent.tools is not None else resolve.builtin_tools()
            keep = set(resolve.read_only_tools()) if value == "read-only" else set()
            agent.tools = [t for t in base if t in keep]
            return
        elif path == "tools.options":
            self._narrow_options(agent, have, value)
            return
        elif path == "instructions.text":
            agent.instructions = (agent.instructions or "").rstrip() + "\n\n" + value
            return
        else:
            raise _refused(path, "this setting cannot be narrowed")
        agent.leaves[path] = value

    @staticmethod
    def _narrow_rules(path: str, have, value: dict) -> dict:
        """The merged rules; raises unless every given rule is at least as strict."""
        have = have or {}
        if path.endswith(".tools"):
            for tool, rule in value.items():
                # No rule yet means the permission mode decides, which may ask; an
                # explicit allow would end that, so it is never a narrowing.
                floor = _RULE_ORDER[have[tool]] if tool in have else _RULE_ORDER["ask"]
                if _RULE_ORDER[rule] < floor:
                    raise _refused(f"{path}.{tool}", f"{rule} is weaker than the current rule")
            return {**have, **value}
        if "allow_prefixes" in value and not set(value["allow_prefixes"]) <= set(
                have.get("allow_prefixes", [])):
            raise _refused(f"{path}.allow_prefixes", "cannot allow more commands")
        merged = {**have}
        for key in ("deny", "ask"):
            if key in value:
                merged[key] = list(dict.fromkeys([*have.get(key, []), *value[key]]))
        if "allow_prefixes" in value:
            merged["allow_prefixes"] = list(value["allow_prefixes"])
        return merged

    @staticmethod
    def _narrow_options(agent, have, value: dict) -> None:
        have = have or {}
        for tool, options in value.items():
            for option, new in options.items():
                old = (have.get(tool) or {}).get(option)
                if option in ("timeout_sec", "max_output_bytes"):
                    if old is not None and new > old:
                        raise _refused(f"tools.options.{tool}.{option}",
                                       f"{new} is more than the current {old}")
                elif option == "allowed_domains":
                    if old is not None and not all(
                            any(d == o or d.endswith("." + o) for o in old) for d in new):
                        raise _refused(f"tools.options.{tool}.{option}",
                                       "may only keep domains inside the current ones")
                else:
                    raise _refused(f"tools.options.{tool}.{option}", "cannot be narrowed")
        merged = {tool: {**(have.get(tool) or {}), **opts} for tool, opts in value.items()}
        agent.leaves["tools.options"] = {**have, **merged}


def Source_narrowed(agent) -> resolve.Source:
    """The provenance marker for narrowed values."""
    return resolve.Source(resolve.INLINE, None, "", "narrowed", agent.source.root)


def coerce(agent, workspace, agents_dir=None) -> AgentSpec | None:
    """An :class:`AgentSpec` for a name-like argument, or ``None`` for a plain name.

    ``agent`` may be an :class:`AgentSpec`, a mapping (inline), or a path to a
    definition file; a plain name stays a name for the ordinary lookup."""
    if isinstance(agent, AgentSpec):
        return agent
    if isinstance(agent, dict):
        return AgentSpec.from_dict(agent, workspace, agents_dir)
    if isinstance(agent, os.PathLike):
        return AgentSpec.from_file(agent, workspace, agents_dir)
    return None
