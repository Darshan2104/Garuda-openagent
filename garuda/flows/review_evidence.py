"""What the real loaders make of changed configuration (#362, #421).

A reviewer reading a diff sees the fields an author wrote, not whether Garuda
reads them. For each changed configuration file in a review candidate, this
runs the ordinary loader on the candidate workspace — YAML parsing and
resolution only, nothing is executed — and records its outcome: loaded with
an effective-config digest and the keys that file supplied, values withheld
because the project file is untrusted, warnings, or the loader's refusal.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import PurePosixPath

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
    if len(parts) >= 3 and parts[0] == ".garuda" and parts[1] == "agents" and name.endswith((".yaml", ".yml", ".md")):
        return "agent-definition"
    return None


def _project_config(workspace: str, path: str) -> dict:
    from garuda.config.garuda_yaml import load_effective

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


def _agent_definition(workspace: str, path: str) -> dict:
    from garuda.agents.setup import resolve_profile

    # Resolved by name through the ordinary project lookup, as a run would select it.
    name = "project/" + PurePosixPath(path).stem
    try:
        profile = resolve_profile(name, workspace, [])
    except Exception as exc:
        return {"status": "refused", "error": str(exc)[:MAX_ERROR_CHARS]}
    declared = sorted(getattr(profile, "declared_fields", None) or [])
    return {"status": "loaded", "name": profile.name, "digest": getattr(profile, "spec_digest", None),
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
