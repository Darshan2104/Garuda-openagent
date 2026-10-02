"""Prompt assembly from labelled sections (plan task H.4, #162).

The static system prompt is an immutable :class:`PromptPlan` built before any
model request, from sections in a fixed, cache-friendly order (the stable
prefix first):

1. the agent's instructions (packaged base, the ``extends`` chain, this agent);
2. user memory: ``<global home>/AGENTS.md`` (``memory.user``);
3. the skills index;
4. project memory files (``memory.project``, ``first`` or ``all``), labelled
   as project text with their path;
5. accepted project notes (``.agent/memory.md``, reviewed by the user, H.9; accepted
   user notes follow user memory as section 2);
6. the durable context pack (``.context/architecture.md`` …) when
   ``memory.context_pack`` is on.

Runtime blocks Garuda adds later (environment snapshot, state card, goals)
are not part of the plan; a run records the digest of what it actually sent.

**Caps.** Each memory file is cut at ``memory.max_chars`` (``memory.truncated``
names the file and the cap; no exact dropped count is invented). The whole
prompt is capped at ``memory.max_total_chars`` and must fit the model's token
budget after the output reserve and safety margin. Over a cap, the context
pack, then project memory shrink, at paragraph boundaries, each with a
diagnostic. Instructions and skills are never cut: a version 1 definition
whose mandatory sections do not fit refuses (``agent.instructions_too_large``,
``agent.prompt_over_budget``).

**Paths.** Project files are read once, without following a symlink, and must
stay inside the workspace; ``..`` and absolute paths refuse.
"""

from __future__ import annotations

import hashlib
import logging
import os
import stat
from dataclasses import dataclass
from pathlib import Path

from garuda.model.config import ConfigError

logger = logging.getLogger(__name__)

CONTEXT_PACK = ("architecture.md", "decisions.md", "discoveries.md", "conventions.md")
MEMORY_TRUNCATED = "memory.truncated"
MEMORY_TRIMMED = "memory.trimmed"
TOKEN_ESTIMATE = 4  # characters per token, the estimator `agent prompt` names
TRIM_ORDER = ("context_pack", "notes", "project_memory")


@dataclass(frozen=True)
class Section:
    kind: str  # instructions | user_memory | skills | project_memory | notes | context_pack
    source: str
    text: str

    @property
    def mandatory(self) -> bool:
        return self.kind in ("instructions", "skills")


@dataclass(frozen=True)
class PromptPlan:
    sections: tuple[Section, ...]
    diagnostics: tuple[dict, ...] = ()

    @property
    def text(self) -> str:
        return "".join(s.text for s in self.sections)

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()


class PromptRefused(ConfigError):
    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code


def _read_inside(path: Path, root: Path, max_chars: int) -> tuple[str, bool] | None:
    """``(text, truncated)``; ``None`` when absent. Refuses an escape."""
    if not (path.exists() or path.is_symlink()):
        return None
    try:
        real = path.resolve(strict=True)
    except OSError:
        return None
    root_real = root.resolve()
    if real != root_real and root_real not in real.parents:
        raise PromptRefused("memory.path_escapes", f"{path} resolves outside {root}")
    # A link that stays inside the root (AGENTS.md -> CLAUDE.md) is read at its
    # target, which itself is opened without following any further link.
    try:
        fd = os.open(real, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as exc:
        raise PromptRefused("memory.path_escapes", f"{path} is a symlink or unreadable: {exc}") \
            from exc
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        data = os.read(fd, max_chars * 4 + 4)
    finally:
        os.close(fd)
    text = data.decode("utf-8", errors="replace")
    return text[:max_chars], len(text) > max_chars


def _relative(rel: str) -> Path:
    path = Path(rel)
    if path.is_absolute() or ".." in path.parts:
        raise PromptRefused("memory.path_escapes", f"{rel} must be a path inside the workspace")
    return path


def _truncated(name: str, path: Path, cap: int, diagnostics: list) -> str:
    diagnostic = {
        "code": MEMORY_TRUNCATED, "file": str(path), "cap_chars": cap,
        "message": f"{name} is longer than {cap} characters; only the first part reaches the model",
        "fix": f"Shorten {name}, or move detail into files the agent can read",
    }
    logger.warning("%s: %s (%s)", MEMORY_TRUNCATED, diagnostic["message"], path)
    diagnostics.append(diagnostic)
    return f"\n\n[project instructions truncated at {cap} characters]"


def _project_memory(profile, root: Path, diagnostics: list) -> list[Section]:
    names = profile.memory_project or ["AGENTS.md", "GARUDA.md"]
    sections = []
    for rel in names:
        path = root / _relative(rel)
        found = _read_inside(path, root, profile.memory_max_chars)
        if found is None:
            continue
        content, cut = found
        if cut:
            content += _truncated(rel, path, profile.memory_max_chars, diagnostics)
        sections.append(Section("project_memory", rel,
                                f"\n\n## Project instructions (from {rel})\n{content}"))
        if profile.memory_project_mode == "first":
            break
    return sections


def _user_memory(profile, diagnostics: list) -> list[Section]:
    if not profile.memory_user:
        return []
    from garuda.config.agent_home import global_settings_path

    home = global_settings_path().expanduser().parent
    path = home / "AGENTS.md"
    found = _read_inside(path, home, profile.memory_max_chars)
    if found is None:
        return []
    content, cut = found
    if cut:
        content += _truncated("AGENTS.md", path, profile.memory_max_chars, diagnostics)
    return [Section("user_memory", str(path), f"\n\n## Your instructions (from {path})\n{content}")]


def _context_pack(profile, root: Path, diagnostics: list) -> list[Section]:
    if not profile.memory_context_pack:
        return []
    sections = []
    for name in CONTEXT_PACK:
        rel = f".context/{name}"
        found = _read_inside(root / rel, root, profile.memory_max_chars)
        if found is None:
            continue
        content, cut = found
        if cut:
            content += _truncated(rel, root / rel, profile.memory_max_chars, diagnostics)
        sections.append(Section("context_pack", rel,
                                f"\n\n## Project context (from {rel})\n{content}"))
    return sections


def _safely(read, strict: bool, diagnostics: list) -> list[Section]:
    """A strict (version 1) definition refuses an escaping memory path; a legacy
    profile skips it with a diagnostic rather than fail a run that used to work."""
    try:
        return read()
    except PromptRefused as exc:
        if strict:
            raise
        logger.warning("%s", exc)
        diagnostics.append({"code": exc.code, "message": str(exc),
                            "fix": "Point it at a file inside the repository"})
        return []


def _trim(section: Section, keep: int) -> Section | None:
    """Cut ``section`` to at most ``keep`` characters at a paragraph or line boundary."""
    if keep <= 0:
        return None
    text = section.text[:keep]
    for boundary in ("\n\n", "\n"):
        at = text.rfind(boundary)
        if at > len(section.text.split("\n", 3)[0]) + 2:
            text = text[:at]
            break
    return Section(section.kind, section.source, text + "\n\n[trimmed to fit the prompt budget]")


def _fit(sections: list[Section], limit: int, what: str, diagnostics: list, strict: bool,
         code: str) -> list[Section]:
    total = sum(len(s.text) for s in sections)
    for kind in TRIM_ORDER:
        for index in range(len(sections) - 1, -1, -1):
            if total <= limit:
                return sections
            section = sections[index]
            if section.kind != kind:
                continue
            over = total - limit
            trimmed = _trim(section, len(section.text) - over - 40)
            diagnostics.append({"code": MEMORY_TRIMMED, "section": section.source,
                                "message": f"{section.source} was shortened to fit {what}",
                                "fix": "Shorten it, or raise memory.max_total_chars"})
            if trimmed is None:
                sections.pop(index)
            else:
                sections[index] = trimmed
            total = sum(len(s.text) for s in sections)
    if total > limit:
        message = (f"the instructions and skills alone are {total} characters, over {what} "
                   f"({limit})")
        if strict:
            raise PromptRefused(code, message)
        logger.warning("%s: %s", code, message)
        diagnostics.append({"code": code, "message": message, "fix": "Shorten the instructions"})
    return sections


def build_plan(profile, workspace_root=None, *, base: str, skills_block: str = "",
               skills_source: str = "-", strict: bool | None = None,
               diagnostics: list | None = None) -> PromptPlan:
    """The prompt plan for ``profile`` in ``workspace_root`` (see module docstring)."""
    found: list[dict] = []
    strict = getattr(profile, "spec_version", None) == 1 if strict is None else strict
    source = str(profile.source_path) if profile.system_prompt else "default"
    sections = [Section("instructions", source, base)]
    sections += _safely(lambda: _user_memory(profile, found), strict, found)
    from garuda.context.notes import prompt_sections as accepted_notes

    notes = accepted_notes(profile, workspace_root, profile.memory_max_chars, found)
    sections += [s for s in notes if s.kind == "user_memory"]
    if skills_block:
        sections.append(Section("skills", skills_source, f"\n\n{skills_block}"))
    if workspace_root:
        root = Path(workspace_root)
        sections += _safely(lambda: _project_memory(profile, root, found), strict, found)
        sections += [s for s in notes if s.kind == "notes"]
        sections += _safely(lambda: _context_pack(profile, root, found), strict, found)
    sections = _fit(sections, profile.memory_max_total_chars, "memory.max_total_chars", found,
                    strict, "agent.instructions_too_large")
    budget = (profile.max_context_tokens - (profile.max_tokens or profile.reserved_output_tokens)
              - profile.context_safety_margin_tokens)
    sections = _fit(sections, max(0, budget) * TOKEN_ESTIMATE, "the model's token budget",
                    found, strict, "agent.prompt_over_budget")
    if diagnostics is not None:
        diagnostics.extend(found)
    return PromptPlan(tuple(sections), tuple(found))
