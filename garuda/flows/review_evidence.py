"""What the real loaders make of changed configuration (#362, #421).

A reviewer reading a diff sees the fields an author wrote, not whether Garuda
reads them. For each changed configuration file in a review candidate, this
runs the ordinary loader on the candidate workspace — YAML parsing and
resolution only, nothing is executed — and records its outcome: loaded with
an effective-config digest and the keys that file supplied, values withheld
because the project file is untrusted, warnings, or the loader's refusal.
An outcome is attributed to a changed file only when the loader read that
file: a nested ``garuda.yaml`` is ``not-loaded``, and an agent definition that
another one of the same name outranks (``.agent/agents`` before
``.garuda/agents``) is ``shadowed`` with the winning path.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath

from garuda.config.agent_home import AGENT_HOME_DIRS, resolve_agents_dirs

MAX_CONFIG_FILES = 10
MAX_KEYS = 50
MAX_ERROR_CHARS = 500


def _digest(value) -> str:
    return "sha256:" + hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def _kind(path: str) -> str | None:
    parts = PurePosixPath(path).parts
    name = parts[-1] if parts else ""
    if name == "garuda.yaml":
        return "project-config"
    if (len(parts) >= 3 and parts[0] in AGENT_HOME_DIRS and parts[1] == "agents"
            and name.endswith((".yaml", ".yml", ".md"))):
        return "agent-definition"
    return None


def _same_file(a: Path, b: Path) -> bool:
    return os.path.realpath(a) == os.path.realpath(b)


def _relative(workspace: str, path: Path) -> str:
    try:
        return Path(os.path.realpath(path)).relative_to(os.path.realpath(workspace)).as_posix()
    except ValueError:
        return str(path)


def _project_config(workspace: str, path: str) -> dict:
    from garuda.config.garuda_yaml import load_effective, project_path

    read = project_path(workspace)
    if not _same_file(Path(workspace) / path, read):
        return {"status": "not-loaded",
                "reason": f"Garuda reads only the project-root garuda.yaml ({read})"}
    if not (Path(workspace) / path).is_file():
        return {"status": "not-loaded", "reason": "the candidate has no such file"}
    try:
        resolved = load_effective(workspace)
    except Exception as exc:  # the loader's refusal is the evidence
        return {"status": "refused", "error": str(exc)[:MAX_ERROR_CHARS]}
    if resolved is None:
        return {"status": "not-loaded", "reason": "no configuration file was found"}
    supplied = sorted(key for key, source in resolved.provenance.items()
                      if "project" in str(source))
    return {"status": "loaded", "config_digest": _digest(resolved.config),
            "project_keys": supplied[:MAX_KEYS], "project_keys_total": len(supplied),
            "withheld": list(resolved.withheld)[:MAX_KEYS],
            "withheld_reason": "project file is not trusted" if resolved.withheld else None}


def _definition_name(path: str) -> str | None:
    """The name the loader finds this file under, or ``None`` when it never looks there.

    The loader reads ``<dir>/<name>.yaml|.yml|.md`` and ``<dir>/<name>/agent.md``.
    """
    parts = PurePosixPath(path).parts
    if len(parts) == 3:
        return PurePosixPath(parts[2]).stem
    if len(parts) == 4 and parts[3] == "agent.md":
        return parts[2]
    return None


def _agent_definition(workspace: str, path: str) -> dict:
    from garuda.agents.resolve import _NAME
    from garuda.agents.setup import resolve_profile

    stem = _definition_name(path)
    if stem is None or not _NAME.match(stem):
        return {"status": "not-loaded",
                "reason": "Garuda does not load this path as an agent definition"}
    if not (Path(workspace) / path).is_file():
        return {"status": "not-loaded", "reason": "the candidate has no such file"}
    # Resolved by name through the ordinary project lookup (.agent/agents before
    # .garuda/agents), as a run would select it; only the file that wins is loaded.
    try:
        profile = resolve_profile("project/" + stem, workspace, resolve_agents_dirs(workspace))
    except Exception as exc:
        return {"status": "refused", "error": str(exc)[:MAX_ERROR_CHARS]}
    source = getattr(profile, "source_path", None)
    if source is None:
        return {"status": "not-loaded", "reason": "the loader recorded no source file"}
    if not _same_file(Path(workspace) / path, source):
        return {"status": "shadowed", "name": profile.name,
                "loaded_from": _relative(workspace, source),
                "reason": "another definition of this name takes precedence"}
    declared = sorted(getattr(profile, "declared_fields", None) or [])
    return {"status": "loaded", "name": profile.name, "source": _relative(workspace, source),
            "digest": getattr(profile, "spec_digest", None),
            "declared": declared[:MAX_KEYS],
            "warnings": [str(w)[:MAX_ERROR_CHARS] for w in (getattr(profile, "warnings", None) or [])][:10]}


def config_evidence(workspace: str, changed: list[str] | None) -> dict:
    """Bounded loader outcomes for the candidate's changed configuration files."""
    if changed is None:
        return {"status": "unknown", "reason": "the changed files were not recorded", "files": []}
    targets = [(p, kind) for p in changed if (kind := _kind(str(p)))]
    files = []
    for path, kind in targets[:MAX_CONFIG_FILES]:
        outcome = (_project_config if kind == "project-config" else _agent_definition)(workspace, str(path))
        files.append({"path": str(path), "kind": kind, **outcome})
    return {"status": "recorded", "files": files, "total": len(targets),
            "complete": len(targets) <= MAX_CONFIG_FILES}
