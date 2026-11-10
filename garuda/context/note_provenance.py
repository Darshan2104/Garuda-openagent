"""Bounded, optional evidence for reviewed notes; workspace reads stay with Environment."""

from __future__ import annotations

import hashlib
import json
import re
import shlex
from datetime import datetime, timezone
from pathlib import PurePosixPath

MAX_SOURCE_BYTES = 1024 * 1024


def normalize(provenance: dict | None) -> dict | None:
    from garuda.context.notes import NotesRefused
    from garuda.context.redact import redact_text

    if provenance is None:
        return None
    try:
        if not isinstance(provenance, dict) or provenance.keys() - {
                "evidence", "sources", "supersedes", "expires_at", "confirmed_at"}:
            raise ValueError
        encoded = json.dumps(provenance, sort_keys=True, ensure_ascii=False)
        if len(encoded) > 4096 or "[REDACTED:" in encoded or redact_text(encoded)[1]:
            raise ValueError
        result = {}
        for key in ("evidence", "supersedes"):
            values = provenance.get(key, [])
            if not isinstance(values, list) or len(values) > 4:
                raise ValueError
            if any(not isinstance(v, str) or not v.strip() or len(v) > 160
                   or any(ord(c) < 32 for c in v) for v in values):
                raise ValueError
            if key == "supersedes" and any(not re.fullmatch(r"[0-9a-f]{32}", v) for v in values):
                raise ValueError
            if values:
                result[key] = sorted(set(values))
        sources = provenance.get("sources", {})
        if not isinstance(sources, dict) or len(sources) > 8:
            raise ValueError
        for path, digest in sources.items():
            source_path(path)
            if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise ValueError
        if sources:
            result["sources"] = dict(sorted(sources.items()))
        for key in ("expires_at", "confirmed_at"):
            if key in provenance:
                result[key] = timestamp(provenance[key]).isoformat()
        return result or None
    except (TypeError, ValueError) as exc:
        raise NotesRefused("memory.invalid_provenance", "invalid or secret-shaped provenance") from exc


def timestamp(value: str) -> datetime:
    if not isinstance(value, str) or len(value) > 40:
        raise ValueError("expected an ISO timestamp with a timezone")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("timestamp needs a timezone")
    return parsed.astimezone(timezone.utc)


def source_path(path: str) -> str:
    if (not isinstance(path, str) or not path or len(path) > 240
            or any(ord(c) < 32 for c in path)):
        raise ValueError("invalid source path")
    parsed = PurePosixPath(path)
    if parsed.is_absolute() or ".." in parsed.parts or str(parsed) != path or path == ".":
        raise ValueError("source paths must be normalized workspace-relative paths")
    return path


async def source_version(env, path: str) -> tuple[str, str | None]:
    """Observe a small regular file through the active workspace owner.

    POSIX tests/wc are available in the supported shell workspaces. No workspace
    Python installation is needed. Errors and ambiguous observations stay unknown.
    """
    from garuda.workspace.protocol import WorkspaceAccessDenied

    source_path(path)
    parts = PurePosixPath(path).parts
    checks = ["[ -x . ] || exit 42"]
    for i in range(1, len(parts) + 1):
        quoted = shlex.quote("./" + "/".join(parts[:i]))
        checks.append(f"[ ! -L {quoted} ] || exit 42")
        checks.append(f"[ -e {quoted} ] || exit 44")
        if i < len(parts):
            checks.append(f"[ -x {quoted} ] || exit 42")
    target = shlex.quote("./" + path)
    checks.extend([f"[ -e {target} ] || exit 44", f"[ -f {target} ] || exit 42",
                   f"[ -r {target} ] || exit 42", f"wc -c < {target}"])
    try:
        result = await env.execute("\n".join(checks), timeout=5, cwd=env.workspace_root)
        if result.truncated:
            return "unknown: source unavailable", None
        if result.exit_code == 44:
            return "stale: source deleted", None
        if result.exit_code != 0 or not re.fullmatch(r"\s*\d+\s*", result.stdout):
            return "unknown: source unavailable", None
        size = int(result.stdout)
        if size > MAX_SOURCE_BYTES:
            return "unknown: source too large", None
        data = await env.read_bytes(path)
        if len(data) != size:
            return "unknown: source changed during read", None
        return "current", hashlib.sha256(data).hexdigest()
    except WorkspaceAccessDenied:
        return "unknown: source access refused", None
    except Exception:
        return "unknown: source unavailable", None


async def capture(env, *, sources=None, evidence=None, supersedes=None, expires_at=None,
                  readable=None) -> dict | None:
    """``readable(path) -> bool`` is the caller's permission screen. Every path
    is screened before any is observed, so one refused source runs nothing."""
    from garuda.context.notes import NotesRefused

    if sources is None:
        sources = []
    if not isinstance(sources, list) or len(sources) > 8:
        raise NotesRefused("memory.invalid_provenance", "at most eight source paths")
    try:
        paths = sorted({source_path(p) for p in sources})
    except (TypeError, ValueError) as exc:
        raise NotesRefused("memory.invalid_provenance", "invalid source paths") from exc
    # Validate all supplied data before executing or reading workspace sources.
    result = normalize({**({"evidence": evidence} if evidence is not None else {}),
                        **({"supersedes": supersedes} if supersedes is not None else {}),
                        **({"expires_at": expires_at} if expires_at is not None else {}),
                        "sources": {p: "0" * 64 for p in paths}}) or {}
    for path in paths:
        if readable is not None and not await readable(path):
            raise NotesRefused("memory.source_refused",
                               "the permission policy does not allow reading a referenced source")
    versions = {}
    for path in paths:
        _status, digest = await source_version(env, path)
        if digest is None:
            raise NotesRefused("memory.source_unavailable", "cannot confirm a referenced source")
        versions[path] = digest
    if versions:
        result["sources"] = versions
        result["confirmed_at"] = datetime.now(timezone.utc).isoformat()
    return result or None
