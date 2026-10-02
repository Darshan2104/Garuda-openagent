"""See what an agent will run with (plan task H.2, #161).

``garuda agent list | show | prompt | check | new``. Inspection is pure: it
reads definitions, skills and configuration files and never imports project
code, connects an MCP server, runs a hook or probe, or calls a model. MCP
tools that only a live server could list are reported ``unknown``, never
guessed. Secrets are redacted unless ``--raw`` asks for the authorized source
text locally; raw text is never persisted.

The ``prompt`` digest covers the *static* first-request prompt. A run records
the *actual* outbound system-message digest at each request boundary
(``system_prompt`` events), which differs once runtime blocks — an
environment snapshot, resume content — are added.
"""

from __future__ import annotations

import dataclasses
import hashlib
import os
from pathlib import Path

from garuda.agents import resolve, spec
from garuda.diagnostics import REGISTRY, Diagnostic, diagnostic

ESTIMATOR = "chars/4"


def _dirs(workspace) -> list[Path]:
    from garuda.config.agent_home import resolve_agents_dirs

    found = resolve_agents_dirs(str(workspace), None)
    if found is None:
        return []
    return [Path(d) for d in (found if isinstance(found, (list, tuple)) else [found])]


def _redact(value, raw: bool):
    if raw:
        return value
    from garuda.context.redact import redact_text

    if isinstance(value, str):
        return redact_text(value)[0]
    if isinstance(value, dict):
        return {k: _redact(v, raw) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(v, raw) for v in value]
    return value


def _label(agent, origin) -> str:
    if origin is None:
        return "default"
    if origin is agent.source:
        return origin.authority
    return f"extends:{origin.qualified}"


# --- list -------------------------------------------------------------------------------


def list_agents(workspace) -> list[dict]:
    from garuda.agents.loader import _profile_names_in_dir

    locations = [(resolve.PROJECT, d) for d in _dirs(workspace)] + [
        (resolve.USER, resolve.user_agents_dir()), (resolve.PACKAGED, resolve._defaults_dir())]
    rows, seen = [], {}
    for authority, directory in locations:
        for name in sorted(_profile_names_in_dir(directory)):
            qualified = {"packaged": "garuda", "user": "user", "project": "project"}[authority]
            row = {"name": name, "source": authority, "qualified": f"{qualified}/{name}",
                   "extends": None, "description": "", "shadowed_by": seen.get(name)}
            try:
                agent = resolve.resolve_agent(f"{qualified}/{name}", _dirs(workspace))
                row["description"] = agent.description
                row["path"] = str(agent.source.path)
                row["extends"] = (agent.chain[-2].qualified if len(agent.chain) > 1 else None)
            except Exception as exc:  # a broken file is listed, with its problem
                row["error"] = str(exc)
            seen.setdefault(name, f"{qualified}/{name}")
            rows.append(row)
    return rows


# --- show -----------------------------------------------------------------------------------


def show(name: str, workspace, *, raw: bool = False) -> dict:
    from garuda.agents.setup import static_agent_config

    agent = resolve.resolve_agent(name, _dirs(workspace))
    profile = resolve.activate(agent)
    fields = {}
    for path, field in spec.FIELDS.items():
        if path in agent.leaves:
            value = agent.leaves[path]
        elif field.profile and hasattr(profile, field.profile):
            value = getattr(profile, field.profile)
        else:
            continue
        fields[path] = {"value": _redact(value, raw),
                        "source": _label(agent, agent.provenance.get(path)),
                        "supported": field.supported}
    fields["instructions"] = {"value": _redact(agent.instructions, raw),
                              "source": _label(agent, agent.provenance.get("instructions"))}
    fields["tools"] = {"value": agent.tools, "source": _label(agent, agent.provenance.get("tools"))}
    from garuda.skills.sources import select

    selection = select(profile, workspace)
    skills = {"selected": [{"name": sk.name, "source": selection.sources[sk.name],
                            "path": str(sk.path)} for sk in selection.skills],
              "shadowed": selection.shadowed, "load": profile.skills_load}
    config = dataclasses.asdict(static_agent_config(profile, workspace))
    prompt_text = config.pop("system_prompt") or ""
    config["system_prompt_digest"] = hashlib.sha256(prompt_text.encode()).hexdigest()
    return {
        "name": agent.name, "source": agent.source.authority,
        "path": str(agent.source.path), "chain": [s.qualified for s in agent.chain],
        "fields": fields, "skills": skills, "config": _redact(config, raw),
        "warnings": agent.warnings,
    }


# --- prompt -----------------------------------------------------------------------------------


def prompt(name: str, workspace, *, raw: bool = False) -> dict:
    from garuda.agents.loader import system_prompt_sections

    profile = resolve.activate(resolve.resolve_agent(name, _dirs(workspace)))
    sections = system_prompt_sections(profile, str(workspace))
    full = "".join(text for _n, _s, text in sections)
    return {
        "name": profile.name, "kind": "static",
        "note": "a run records the actual system-message digest per request; runtime "
                "blocks (environment snapshot, resume content) make it differ",
        "digest": hashlib.sha256(full.encode("utf-8")).hexdigest(),
        "estimator": ESTIMATOR,
        "sections": [{"section": n, "source": src, "bytes": len(t.encode("utf-8")),
                      "chars": len(t), "tokens": len(t) // 4, "text": _redact(t, raw)}
                     for n, src, t in sections],
    }


# --- check -------------------------------------------------------------------------------------


def _from_error(exc: Exception) -> Diagnostic:
    code = getattr(exc, "code", None) or "agent.invalid_value"
    if code not in REGISTRY:
        code = "agent.invalid_value"
    return diagnostic(code, str(exc), level="error", path=getattr(exc, "path", "") or "(file)")


def check(target: str, workspace) -> list[Diagnostic]:
    """Every diagnostic for one agent (a name or a definition file path)."""
    from garuda.agents.authority import enforce_project_ceiling
    from garuda.config.agent_home import resolve_agent_home
    from garuda.model.config import ConfigError

    out: list[Diagnostic] = []
    try:
        if os.sep in target or target.endswith((".yaml", ".yml", ".md")):
            path = Path(target)
            data = resolve._read(path, path.parent)
            source = resolve.Source(resolve.INLINE, path, hashlib.sha256(data).hexdigest(),
                                    f"project/{path.stem}", path.parent)
            agent = resolve.resolve_source(source, data, _dirs(workspace))
        else:
            agent = resolve.resolve_agent(target, _dirs(workspace))
        profile = resolve.activate(agent)
        resolve.check_references(profile, workspace)
        enforce_project_ceiling(profile_name=profile.name, declared_mode=profile.permission_mode,
                                source_path=profile.source_path, workspace=workspace,
                                explicit_permission_mode=None,
                                global_settings=resolve_agent_home(workspace).global_settings)
    except (ConfigError, OSError, UnicodeDecodeError) as exc:
        return [_from_error(exc)]
    for warning in agent.warnings:
        out.append(diagnostic("agent.legacy_warning", warning, level="warning"))
    from garuda.skills.sources import select, tool_gaps

    selection = select(profile, workspace)
    for name, missing in tool_gaps(selection.skills, profile.tools):
        out.append(diagnostic("skill.tool_not_granted",
                              f"skill {name} names {', '.join(missing)}, which this agent lacks "
                              "(allowed-tools is advisory)", level="warning", skill=name,
                              tools=", ".join(missing)))
    if profile.mcp_servers:
        out.append(diagnostic("agent.mcp_tools_unknown",
                              f"tools of {', '.join(profile.mcp_servers)} are known only to a "
                              "running server", servers=", ".join(profile.mcp_servers)))
    out.append(diagnostic("agent.ok", f"{agent.source.qualified} resolves "
                                      f"({' -> '.join(s.qualified for s in agent.chain)})"))
    return out


# --- new -----------------------------------------------------------------------------------------


def new(name: str, workspace, *, from_agent: str = "garuda/build", project: bool = False) -> Path:
    """Write a minimal definition; refuses to overwrite."""
    resolve._split(name)
    resolve._split(from_agent)
    directory = (Path(workspace) / ".agent" / "agents") if project else resolve.user_agents_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.yaml"
    text = (f"version: 1\nextends: {from_agent}\ninstructions:\n  text: |\n"
            f"    Describe how {name} should work differently from {from_agent}.\n")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o644)
    try:
        os.write(fd, text.encode())
    finally:
        os.close(fd)
    return path
