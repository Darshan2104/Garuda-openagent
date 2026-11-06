"""Bounded, inspection-only inventory of instruction text; grants no authority."""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path

NAMES = ("AGENTS.md", "GARUDA.md", "CLAUDE.md")
SKIP_DIRS = {".git", ".hg", ".svn", ".venv", "venv", "node_modules"}
MAX_ENTRIES = 10_000
MAX_FILES = 256
MAX_DEPTH = 16
MAX_BYTES = 256 * 1024
TOTAL_BYTES = 2 * 1024 * 1024
MAX_CONFLICTS = 256
NOT_IMPORTED = ["hooks", "tools", "MCP servers", "permissions"]


def _open_inside(root: Path, relative: Path) -> int:
    """Open each component without following links after canonicalization."""
    flags = os.O_RDONLY | os.O_NOFOLLOW
    fd = os.open(root, flags | os.O_DIRECTORY)
    try:
        for part in relative.parts[:-1]:
            child = os.open(part, flags | os.O_DIRECTORY, dir_fd=fd)
            os.close(fd)
            fd = child
        return os.open(relative.name, flags | os.O_NONBLOCK, dir_fd=fd)
    finally:
        os.close(fd)


def _discover(root: Path, problems: list) -> list[Path]:
    pending, found, remaining = [Path(".")], [], MAX_ENTRIES
    while pending:
        relative = pending.pop(0)
        try:
            fd = _open_inside(root, relative) if relative.parts else os.open(
                root, os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY)
            try:
                with os.scandir(fd) as entries:
                    names = []
                    for entry in entries:
                        names.append((entry.name, entry.is_dir(follow_symlinks=False),
                                      entry.is_symlink()))
                        if len(names) > remaining:
                            break
            finally:
                os.close(fd)
        except OSError:
            problems.append({"path": str(relative), "reason": "directory unreadable or changed"})
            continue
        remaining -= len(names)
        if remaining < 0:
            problems.append({"path": str(relative), "reason": "entry limit; discovery incomplete"})
            break
        for name, directory, link in sorted(names):
            path = relative / name
            if name in NAMES and not directory:
                found.append(root / path)
                if len(found) >= MAX_FILES:
                    problems.append({"path": ".", "reason": "source limit; discovery incomplete"})
                    return found
            elif directory and name not in SKIP_DIRS:
                if len(path.parts) <= MAX_DEPTH:
                    pending.append(path)
                else:
                    problems.append({"path": str(path), "reason": "depth limit"})
            elif link and name not in NAMES:
                problems.append({"path": str(path), "reason": "unfollowed symlink"})
    return found


def _metadata(data: bytes) -> tuple[list[str] | None, str]:
    from garuda.agents.frontmatter import _FRONTMATTER_RE

    text = data.decode("utf-8", errors="replace").replace("\r\n", "\n")
    if text.partition("\n")[0].rstrip() != "---":
        return [], "plain text; no structured fields mapped"
    match = _FRONTMATTER_RE.match(text)
    if match is None or len(match.group(1)) > 16 * 1024:
        return None, "unknown: malformed or oversized front matter"
    raw = match.group(1)
    import yaml

    from garuda.agents.resolve import _yaml

    try:
        depth = 0
        for event in yaml.parse(raw):
            if isinstance(event, yaml.AliasEvent):
                return None, "unknown: aliased front matter"
            if isinstance(event, (yaml.MappingStartEvent, yaml.SequenceStartEvent)):
                depth += 1
            elif isinstance(event, (yaml.MappingEndEvent, yaml.SequenceEndEvent)):
                depth -= 1
            if depth > MAX_DEPTH:
                return None, "unknown: deeply nested front matter"
        meta = _yaml(raw, "instruction front matter")
        if not isinstance(meta, dict):
            return None, "unknown: front matter is not a mapping"
        return sorted(str(key) for key in meta), "unsupported/ignored as configuration; text only"
    except (ValueError, TypeError, RecursionError, yaml.YAMLError):
        return None, "unknown: invalid front matter"


def inventory(agent, workspace, sections: list[dict] | None) -> dict:
    """Use the final static prompt sections as evidence, never foreign precedence rules."""
    from garuda.config.agent_home import global_settings_path

    root = Path(workspace).resolve()
    home = global_settings_path().expanduser().parent.resolve()
    problems: list[dict] = []
    paths = [(path, root, "project") for path in _discover(root, problems)]
    paths.append((home / "AGENTS.md", home, "user"))
    profile = agent.to_profile()
    configured = profile.memory_project or ["AGENTS.md", "GARUDA.md"]
    for rel in configured:
        relative = Path(rel)
        if not relative.is_absolute() and ".." not in relative.parts:
            paths.append((root / relative, root, "project"))
    for path in agent.instruction_files:
        owner = next((source for source in reversed(agent.chain)
                      if source.root and path.is_relative_to(source.root)), None)
        if owner:
            paths.append((path, owner.root.resolve(), owner.authority))
    loaded = []
    for index, section in enumerate(sections or []):
        kind, source = section["section"], section["source"]
        if kind == "project_memory":
            loaded.append((root / source, source, index))
        elif kind == "user_memory" and Path(source).name == "AGENTS.md":
            loaded.append((Path(source), source, index))
        elif kind == "instructions":
            loaded.extend((path, source, index) for path in agent.instruction_files)
    candidates = {(str(path), str(boundary)): (path, boundary, scope)
                  for path, boundary, scope in paths}
    rows, seen, remaining = [], {}, TOTAL_BYTES
    ordered = sorted(candidates.values(), key=lambda item: (
        item[2] != "user", len(item[0].parts),
        configured.index(str(item[0].relative_to(root)))
        if item[0].is_relative_to(root) and str(item[0].relative_to(root)) in configured
        else len(configured), str(item[0])))
    for path, boundary, scope in ordered[:MAX_FILES]:
        row = {"path": str(path), "scope": scope, "directory": str(path.parent),
               "loaded": None if sections is None else False, "loaded_via": [],
               "precedence": None, "shadowed_by": None, "duplicate_of": None,
               "digest": None, "unsupported_fields": None,
               "executable_configuration": {"status": "not imported", "categories": NOT_IMPORTED}}
        try:
            canonical = path.resolve(strict=True)
            if not canonical.is_relative_to(boundary):
                row["status"] = "refused: target outside source scope"
                rows.append(row)
                continue
            row["canonical_path"] = str(canonical)
            fd = _open_inside(boundary, canonical.relative_to(boundary))
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode):
                    row["status"] = "refused: not a regular file"
                    rows.append(row)
                    continue
                allowance = min(MAX_BYTES + 1, remaining)
                data = b""
                while len(data) < allowance:
                    chunk = os.read(fd, min(64 * 1024, allowance - len(data)))
                    if not chunk:
                        break
                    data += chunk
                remaining -= len(data)
                after = os.fstat(fd)
            finally:
                os.close(fd)
        except FileNotFoundError:
            if path.is_symlink():
                row["status"] = "refused: broken symlink"
                rows.append(row)
            continue
        except (OSError, RuntimeError, ValueError):
            row["status"] = "unknown: unreadable or changed source"
            rows.append(row)
            continue
        stable = (info.st_size, info.st_mtime_ns, info.st_ctime_ns) == (
            after.st_size, after.st_mtime_ns, after.st_ctime_ns)
        row["status"] = ("read" if stable and len(data) == info.st_size and len(data) <= MAX_BYTES
                         and allowance > 0 else "unknown: read limit or changed source")
        if row["status"] == "read":
            row["digest"] = hashlib.sha256(data).hexdigest()
            row["unsupported_fields"], row["field_status"] = _metadata(data)
            identity = (info.st_dev, info.st_ino)
            row["duplicate_of"] = seen.get(identity) or seen.get(row["digest"])
            seen.setdefault(identity, str(path))
            seen.setdefault(row["digest"], str(path))
        for selected, source, index in loaded:
            try:
                same = canonical == selected.resolve(strict=True)
            except (OSError, RuntimeError):
                same = False
            if same:
                row["loaded"] = True
                row["loaded_via"].append(source)
                row["precedence"] = index
        relative = str(path.relative_to(root)) if scope == "project" else None
        row["configured_order"] = configured.index(relative) if relative in configured else None
        if profile.memory_project_mode == "first" and row["configured_order"] is not None:
            winner = next((source for _path, source, _index in loaded if source in configured), None)
            if winner and configured.index(winner) < row["configured_order"]:
                row["shadowed_by"] = str(root / winner)
        rows.append(row)
    if len(ordered) > MAX_FILES:
        problems.append({"path": ".", "reason": "source limit; discovery incomplete"})
    conflicts = []
    conflicts_complete = True
    for index, row in enumerate(rows):
        for other in rows[:index]:
            if not row["digest"] or not other["digest"] or row["digest"] == other["digest"]:
                continue
            a, b = Path(row["directory"]), Path(other["directory"])
            if row["scope"] == "user" or other["scope"] == "user" or a.is_relative_to(b) or b.is_relative_to(a):
                if len(conflicts) >= MAX_CONFLICTS:
                    conflicts_complete = False
                    break
                conflicts.append({"paths": [other["path"], row["path"]],
                                  "kind": "different text in overlapping scopes",
                                  "semantic_conflict": "unknown"})
        if not conflicts_complete:
            problems.append({"path": ".", "reason": "conflict limit; relationships incomplete"})
            break
    partial = problems or any(row["status"] != "read" or row["unsupported_fields"] is None
                              for row in rows)
    return {"sources": rows, "conflicts": conflicts, "coverage": "partial" if partial else "complete",
            "problems": problems, "formats": list(NAMES), "excluded_directories": sorted(SKIP_DIRS),
            "project_mode": profile.memory_project_mode,
            "note": "Current native static prompt only; external harness loading and semantic "
                    "conflicts are unknown. Nested text has no automatic precedence. "
                    "Loaded sections can contain capped or trimmed text; see prompt diagnostics. "
                    "Foreign configuration is never imported."}
