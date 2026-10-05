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
    if origin.qualified == "narrowed":
        return "narrowed"
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
                row["error_code"] = dashboard_problem(code=getattr(exc, "code", None))["error_code"]
            seen.setdefault(name, f"{qualified}/{name}")
            rows.append(row)
    return rows


# --- show -----------------------------------------------------------------------------------


def _target(target: str, workspace) -> resolve.ResolvedAgent:
    """A name, or a path to a definition file (``--agent-file``), resolved purely."""
    # A qualified name (`garuda/build`, `project/x`) has a separator too: it is a file only when
    # it carries a definition extension or names a file that exists.
    if target.endswith((".yaml", ".yml", ".md")) or (os.sep in target and os.path.isfile(target)):
        from garuda.agents.spec_api import AgentSpec

        return AgentSpec.from_file(target, workspace).resolved()
    return resolve.resolve_agent(target, _dirs(workspace))


def show(name: str, workspace, *, raw: bool = False) -> dict:
    return show_agent(_target(name, workspace), workspace, raw=raw)


def show_agent(agent: resolve.ResolvedAgent, workspace, *, raw: bool = False) -> dict:
    """``show`` for an already-resolved agent: the one definition every entry point reads."""
    from garuda.agents.setup import static_agent_config
    from garuda.agents.spec_api import _digest

    profile = resolve.activate(agent)
    profile.spec_digest = _digest(agent)
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
    from garuda.agents.spec_api import _digest

    return {
        "name": agent.name, "source": agent.source.authority,
        "path": str(agent.source.path), "chain": [s.qualified for s in agent.chain],
        "digest": _digest(agent),
        "fields": fields, "skills": skills, "config": _redact(config, raw),
        "warnings": agent.warnings,
    }


# --- prompt -----------------------------------------------------------------------------------


def prompt(name: str, workspace, *, raw: bool = False) -> dict:
    return prompt_agent(_target(name, workspace), workspace, raw=raw)


def prompt_agent(agent: resolve.ResolvedAgent, workspace, *, raw: bool = False) -> dict:
    from garuda.agents.loader import system_prompt_sections

    profile = resolve.activate(agent)
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


# --- dashboard ----------------------------------------------------------------------------------


def _brief(value, limit: int = 120) -> str:
    import json

    text = value if isinstance(value, str) else json.dumps(value, default=str, sort_keys=True)
    return text if len(text) <= limit else text[: limit - 1] + "\u2026"


def dashboard_problem(*, code: object = None) -> dict:
    """Project an inspection failure without its private source diagnostic."""
    if not isinstance(code, str) or code not in REGISTRY or not code.startswith(("agent.", "memory.")):
        code = "agent.invalid_value"
    return {"error_code": code,
            "error": "Agent inspection failed. Run garuda agent check locally for details."}


def dashboard_rows(workspace) -> list[dict]:
    """Every agent for the Setup view: source, the settings it declares (with where each came
    from), its definition digest, the static prompt digest and the size of each prompt section.

    Read-only and bounded: no instruction or prompt text is returned (only its size and digest),
    values are redacted, and a definition that fails to resolve is listed with a source-free
    diagnostic code/message (detailed errors and warnings remain local)
    instead of hiding the others. Nothing is started."""
    rows = []
    for listed in list_agents(workspace):
        row = {k: listed.get(k) for k in ("name", "source", "qualified", "extends",
                                          "description", "shadowed_by")}
        row["description"] = _redact(row["description"], False)
        if listed.get("error"):
            row.update(dashboard_problem(code=listed.get("error_code")))
            rows.append(row)
            continue
        try:
            dirs = _dirs(workspace)
            info = show_agent(resolve.resolve_agent(listed["qualified"], dirs), workspace)
            agent = resolve.resolve_agent(listed["qualified"], dirs)
            sections = prompt_agent(agent, workspace)
        except Exception as exc:  # one broken definition does not hide the rest
            row.update(dashboard_problem(code=getattr(exc, "code", None)))
            rows.append(row)
            continue
        declared = set(agent.leaves) | {"tools"}
        row.update(
            digest=info["digest"], prompt_digest=sections["digest"], estimator=ESTIMATOR,
            versioned=agent.versioned,
            warnings=(["Legacy definition has warnings. Run garuda agent check locally for details."]
                      if info["warnings"] else []),
            instructions_chars=len(agent.instructions or ""),
            sections=[{k: s[k] for k in ("section", "source", "bytes", "chars", "tokens")}
                      for s in sections["sections"]],
            tokens=sum(s["tokens"] for s in sections["sections"]),
            fields=[{"path": path, "value": _brief(field["value"]), "source": field["source"]}
                    for path, field in info["fields"].items()
                    if path in declared and path != "description"])
        rows.append(row)
    return rows


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
