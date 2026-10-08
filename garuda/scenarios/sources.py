"""Contained file references and authorized bounded session briefs, without writes."""

from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import asdict
from pathlib import Path

from garuda.context import brief, tags
from garuda.core.sessions import SessionStore
from garuda.scenarios.types import StarterError
from garuda.workspace.paths import resolve_workspace_path

MAX_SOURCES = 16
MAX_SOURCE_BYTES = 16 * 1024 * 1024


def _file(ref: str, root: Path) -> dict:
    name, separator, section = ref.partition("#")
    if (not name or Path(name).is_absolute() or name.startswith("~") or "\x00" in ref
            or (separator and not section.strip())):
        raise StarterError("starter.source_invalid", "sources must be repository-relative files with an optional section")
    try:
        logical = resolve_workspace_path(root, name, confine=True)
        physical = logical.resolve(strict=True)
        relative = physical.relative_to(root)
    except (OSError, ValueError, RuntimeError) as exc:
        raise StarterError("starter.source_escapes", "source is missing or leaves the workspace") from exc
    # Walk the resolved contained path by descriptors: a parent swapped for a
    # symlink after resolution must not redirect this read outside the root.
    try:
        directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as exc:
        raise StarterError("starter.source_invalid", "workspace is unavailable") from exc
    try:
        for part in relative.parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
                            dir_fd=directory)
            os.close(directory)
            directory = child
        fd = os.open(relative.name, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0),
                     dir_fd=directory)
        try:
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode):
                raise StarterError("starter.source_invalid", "source is not a regular file")
            digest, total = hashlib.sha256(), 0
            while data := os.read(fd, 65536):
                total += len(data)
                if total > MAX_SOURCE_BYTES:
                    raise StarterError("starter.source_too_large", "source exceeds 16 MiB; select a smaller file")
                digest.update(data)
            after = os.fstat(fd)
            if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                    after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                raise StarterError("starter.source_changed", "source changed during preview; preview again")
        finally:
            os.close(fd)
    except OSError as exc:
        raise StarterError("starter.source_invalid", "cannot read a stable contained source") from exc
    finally:
        os.close(directory)
    return {"kind": "file", "path": logical.relative_to(root).as_posix(),
            "resolved_path": relative.as_posix(), "section": section if separator else None,
            "sha256": digest.hexdigest(), "bytes": total, "read_by_agent": False}


def resolve_sources(refs: list[str], workspace: Path, *, store: SessionStore | None = None,
                    allow_cross_project_context: bool = False) -> tuple[list[dict], str]:
    if (not isinstance(refs, list) or len(refs) > MAX_SOURCES
            or any(not isinstance(r, str) or not r or len(r) > 1024 or "\x00" in r for r in refs)):
        raise StarterError("starter.source_invalid", "supply at most 16 non-empty source references (1024 characters each)")
    manifest, with_refs, with_ids = [], [], []
    for ref in refs:
        if ref.startswith("session-id:"):
            with_ids.append(ref.removeprefix("session-id:"))
        elif ref.startswith("session:"):
            with_refs.append(ref.removeprefix("session:"))
        else:
            manifest.append(_file(ref, workspace))
    session_tags = tags.resolve(store or SessionStore(), workspace, with_refs=with_refs,
                                with_ids=with_ids, allow_cross_project=allow_cross_project_context,
                                read_only=True)
    briefs = []
    for tag in session_tags:
        item = brief.build_brief(store or SessionStore(), tag.session_id)
        briefs.append(item)
        manifest.append({"kind": "session", **asdict(tag), "sha256": item.fingerprint(),
                         "checks_current": False, "redactions": item.redactions})
    rendered = brief.render(briefs)
    if rendered.trimmed:
        manifest.append({"kind": "session-trimming", "fields": rendered.trimmed})
    return manifest, rendered.text
