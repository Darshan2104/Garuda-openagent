"""Skill sources and per-agent selection (plan task H.5, #163).

Sources, in precedence order: **project** (``.agent/skills``,
``.garuda/skills`` and the agent's ``skills.dirs``), **user**
(``<global home>/skills``, new) and **packaged**. When two sources define the
same name, the nearer one wins and the others are recorded as shadowed.
``skills.from`` limits the sources, ``include`` (``null``: all; ``[]``: none)
and ``exclude`` filter by name, and ``load`` puts an index (read on demand)
or every body into the prompt.

A skill's ``allowed-tools`` stays advisory: an instruction to the model, not
enforcement. ``garuda agent check`` reports ``skill.tool_not_granted`` when it
names a tool the agent lacks. Project skills are project text and grant
nothing. Each skill file is read once, bounded, without following a symlink,
and must stay inside its source directory.

A legacy profile keeps exactly its old sources: project directories and its
``skills_dirs``.
"""

from __future__ import annotations

import logging
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path

from garuda.skills.loader import Skill, _parse_allowed_tools

logger = logging.getLogger(__name__)

MAX_SKILL_BYTES = 256 * 1024
PROJECT, USER, PACKAGED = "project", "user", "packaged"
DEFAULT_FROM = (PROJECT, USER, PACKAGED)


@dataclass
class Selection:
    skills: list[Skill]
    sources: dict[str, str] = field(default_factory=dict)  # name -> source
    shadowed: list[dict] = field(default_factory=list)
    unknown: list[str] = field(default_factory=list)


def user_skills_dir() -> Path:
    from garuda.config.agent_home import global_settings_path

    return global_settings_path().expanduser().parent / "skills"


def packaged_skills_dir() -> Path:
    return Path(__file__).parent / "defaults"


def source_dirs(profile, workspace) -> list[tuple[str, Path]]:
    from garuda.config.agent_home import resolve_agent_home

    dirs: list[tuple[str, Path]] = []
    if workspace:
        dirs += [(PROJECT, Path(d)) for d in resolve_agent_home(workspace).skills_dirs]
    for raw in profile.skills_dirs or []:
        candidate = Path(raw)
        if not candidate.is_absolute() and workspace:
            candidate = Path(workspace) / candidate
        dirs.append((PROJECT, candidate))
    versioned = getattr(profile, "spec_version", None) == 1
    chosen = profile.skills_from if profile.skills_from is not None else (
        list(DEFAULT_FROM) if versioned else [PROJECT])
    dirs += [(USER, user_skills_dir()), (PACKAGED, packaged_skills_dir())]
    return [(source, d) for source, d in dirs if source in chosen]


def _read(path: Path, root: Path) -> str | None:
    try:
        real = path.resolve(strict=True)
    except OSError:
        return None
    root_real = root.resolve()
    if root_real not in real.parents:
        logger.warning("skill %s resolves outside %s; skipped", path, root)
        return None
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        logger.warning("skill %s is a symlink or unreadable; skipped", path)
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        data = os.read(fd, MAX_SKILL_BYTES + 1)
    finally:
        os.close(fd)
    if len(data) > MAX_SKILL_BYTES:
        logger.warning("skill %s is over %d bytes; skipped", path, MAX_SKILL_BYTES)
        return None
    return data.decode("utf-8", errors="replace")


def _load(path: Path, root: Path) -> Skill | None:
    from garuda.agents.frontmatter import parse_frontmatter

    text = _read(path, root)
    if text is None:
        return None
    meta, body = parse_frontmatter(text)
    return Skill(name=meta.get("name", path.parent.name), description=meta.get("description", ""),
                 body=body, path=path, allowed_tools=_parse_allowed_tools(meta.get("allowed-tools")))


def _discover(root: Path) -> list[Skill]:
    if not root.is_dir():
        return []
    found, seen = [], set()
    for pattern in ("SKILL.md", "skill.md", "**/SKILL.md", "**/skill.md"):
        for path in root.glob(pattern):
            if path.is_symlink() or not path.is_file():
                continue
            skill = _load(path, root)
            if skill is not None and skill.name not in seen:
                seen.add(skill.name)
                found.append(skill)
    return found


def select(profile, workspace) -> Selection:
    """The skills this agent gets, with every winner's source and shadowed copy."""
    selection = Selection(skills=[])
    winners: dict[str, Skill] = {}
    for source, directory in source_dirs(profile, workspace):
        for skill in _discover(directory):
            if skill.name in winners:
                selection.shadowed.append({"name": skill.name, "source": source,
                                           "path": str(skill.path),
                                           "by": selection.sources[skill.name]})
                continue
            winners[skill.name] = skill
            selection.sources[skill.name] = source
    include, exclude = profile.skills, set(profile.skills_exclude or [])
    names = sorted(winners) if include is None else [n for n in include if n in winners]
    if include is not None:
        selection.unknown = [n for n in include if n not in winners]
    selection.skills = [winners[n] for n in sorted(names) if n not in exclude]
    return selection


def tool_gaps(skills: list[Skill], granted: list[str] | None) -> list[tuple[str, list[str]]]:
    """``(skill, tools it names that the agent lacks)``; ``granted=None`` means all."""
    if granted is None:
        return []
    have = set(granted)
    gaps = []
    for skill in skills:
        missing = [t for t in (skill.allowed_tools or []) if t not in have]
        if missing:
            gaps.append((skill.name, missing))
    return gaps
