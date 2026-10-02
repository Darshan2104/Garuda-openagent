"""``garuda agent migrate PATH [--write]`` (plan task H.1, #159).

Previews a legacy profile as a version 1 definition and the semantic
difference between the two (it should be none). ``--write`` replaces the file
atomically, keeping a timestamped backup, and refuses if the file changed
since it was read (no clobber). In ``agent.md`` the body is kept as it was.
Where version 1 is stricter — an unknown tool it now refuses, say — the
difference is shown as safety tightening and ``--write`` needs
``--accept-tightening``. A version 1 file is left alone, so repeating
``--write`` changes nothing.
"""

from __future__ import annotations

import dataclasses
import datetime as _dt
import hashlib
import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from garuda.agents import resolve, spec


@dataclass
class Migration:
    path: Path
    text: str | None  # None: already version 1
    digest: str
    diff: list[str] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)
    tightening: list[str] = field(default_factory=list)


def _profile_dict(profile) -> dict:
    data = dataclasses.asdict(profile)
    for key in ("source_path", "spec_version"):
        data.pop(key, None)
    data["declared_fields"] = sorted(data["declared_fields"])
    return data


def _resolve_bytes(path: Path, data: bytes):
    source = resolve.Source(resolve.INLINE, path, hashlib.sha256(data).hexdigest(),
                            f"project/{path.stem}", path.parent)
    return resolve.resolve_source(source, data, [])


def plan(path: str | Path) -> Migration:
    target = Path(path)
    data = resolve._read(target, target.parent)
    digest = hashlib.sha256(data).hexdigest()
    source = resolve.Source(resolve.INLINE, target, digest, f"project/{target.stem}",
                            target.parent)
    doc, warnings, _declared, is_v1 = resolve.parse_source(source, data)
    if is_v1:
        return Migration(target, None, digest)
    body = None
    if target.suffix == ".md":
        instructions = dict(doc.get("instructions") or {})
        body = instructions.pop("text", None)
        if instructions:
            doc["instructions"] = instructions
        else:
            doc.pop("instructions", None)
    rendered = yaml.safe_dump(doc, sort_keys=False, default_flow_style=False, allow_unicode=True)
    text = f"---\n{rendered}---\n\n{body}\n" if target.suffix == ".md" else rendered
    if target.suffix == ".md" and body is None:
        text = f"---\n{rendered}---\n"
    migration = Migration(target, text, digest, dropped=warnings)
    before = _profile_dict(resolve.activate(_resolve_bytes(target, data)))
    try:
        after = _profile_dict(resolve.activate(_resolve_bytes(target, text.encode())))
    except spec.AgentSpecError as exc:
        migration.tightening.append(str(exc))
        return migration
    for key in sorted(set(before) | set(after)):
        if before.get(key) != after.get(key):
            migration.diff.append(f"~ {key}: {before.get(key)!r} -> {after.get(key)!r}")
    return migration


def write(migration: Migration) -> Path:
    """Replace the file with its version 1 form; returns the backup path."""
    current = hashlib.sha256(migration.path.read_bytes()).hexdigest()
    if current != migration.digest:
        raise spec.AgentSpecError("agent.migrate_conflict", "",
                                  f"{migration.path} changed since it was read; run again")
    stamp = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = migration.path.with_name(f"{migration.path.name}.bak-{stamp}")
    fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, migration.path.read_bytes())
    finally:
        os.close(fd)
    tmp = migration.path.with_name(f".{migration.path.name}.{os.getpid()}.tmp")
    tmp.write_text(migration.text, encoding="utf-8")
    os.replace(tmp, migration.path)
    return backup
