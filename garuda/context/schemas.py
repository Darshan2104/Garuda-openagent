"""Versioned schemas for the generated context files (P0.8, issue #16).

`current-task.md` is the active state; `handoff.md` is the transfer package.
Both are Markdown with YAML frontmatter: human-readable and hand-editable, but
validated on read. Unknown fields round-trip safely; an unknown *version* is
rejected — the reader cannot know what a newer writer meant.

Bounds follow `state_card.py`: generated files are re-read every turn, so an
unbounded one taxes every future turn of the run.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import yaml

from garuda.agents.frontmatter import parse_frontmatter

#: Schema version written by this code.
CONTEXT_SCHEMA_VERSION = 1

MAX_CRITERIA = 20
MAX_FILES = 40
MAX_EVIDENCE = 20
MAX_BLOCKERS = 10
MAX_TEXT_CHARS = 4_000
MAX_TASK_CHARS = 1_500


class ContextSchemaError(ValueError):
    """A generated context file failed validation. Fail-closed, actionable."""


def _require_str(data: dict, key: str, *, where: str, max_chars: int) -> str:
    value = data.get(key, "")
    if not isinstance(value, str):
        raise ContextSchemaError(f"{where}.{key}: must be a string")
    if len(value) > max_chars:
        raise ContextSchemaError(
            f"{where}.{key}: {len(value)} chars exceeds bound of {max_chars}"
        )
    return value


def _require_str_list(data: dict, key: str, *, where: str, limit: int) -> list[str]:
    value = data.get(key, [])
    if not isinstance(value, list) or any(not isinstance(v, str) for v in value):
        raise ContextSchemaError(f"{where}.{key}: must be a list of strings")
    if len(value) > limit:
        raise ContextSchemaError(
            f"{where}.{key}: {len(value)} entries exceeds bound of {limit}"
        )
    return list(value)


@dataclass(frozen=True)
class CurrentTask:
    task: str
    acceptance_criteria: tuple[str, ...] = ()
    changed_files: tuple[str, ...] = ()
    evidence: tuple[str, ...] = ()
    blockers: tuple[str, ...] = ()
    next_action: str = ""
    source_runtime: str = "native"
    extra: dict[str, Any] = field(default_factory=dict)

    def to_frontmatter(self) -> dict[str, Any]:
        return {
            "version": CONTEXT_SCHEMA_VERSION,
            "source_runtime": self.source_runtime,
            "task": self.task,
            "acceptance_criteria": list(self.acceptance_criteria),
            "changed_files": list(self.changed_files),
            "evidence": list(self.evidence),
            "blockers": list(self.blockers),
            "next_action": self.next_action,
            **self.extra,
        }


@dataclass(frozen=True)
class Handoff:
    task: str
    source_runtime: str
    acceptance_criteria: tuple[str, ...] = ()
    changed_files: tuple[str, ...] = ()
    evidence: tuple[str, ...] = ()
    blockers: tuple[str, ...] = ()
    next_action: str = ""
    garuda_session_id: str = ""
    native_session_id: str = ""
    redacted: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    def to_frontmatter(self) -> dict[str, Any]:
        return {
            "version": CONTEXT_SCHEMA_VERSION,
            "source_runtime": self.source_runtime,
            "task": self.task,
            "acceptance_criteria": list(self.acceptance_criteria),
            "changed_files": list(self.changed_files),
            "evidence": list(self.evidence),
            "blockers": list(self.blockers),
            "next_action": self.next_action,
            "garuda_session_id": self.garuda_session_id,
            "native_session_id": self.native_session_id,
            "redacted": self.redacted,
            **self.extra,
        }


_KNOWN = frozenset(
    {
        "version",
        "source_runtime",
        "task",
        "acceptance_criteria",
        "changed_files",
        "evidence",
        "blockers",
        "next_action",
        "garuda_session_id",
        "native_session_id",
        "redacted",
    }
)


def _check_version(meta: dict, *, where: str) -> None:
    version = meta.get("version", CONTEXT_SCHEMA_VERSION)
    if version != CONTEXT_SCHEMA_VERSION:
        raise ContextSchemaError(
            f"{where}: schema v{version} is newer than supported "
            f"v{CONTEXT_SCHEMA_VERSION}; upgrade Garuda to read this file"
        )


def _common(meta: dict, *, where: str, require_source: bool) -> dict[str, Any]:
    _check_version(meta, where=where)
    if require_source and not meta.get("source_runtime"):
        raise ContextSchemaError(f"{where}.source_runtime: required")
    if "redacted" in meta and not isinstance(meta["redacted"], bool):
        raise ContextSchemaError(f"{where}.redacted: must be a boolean")
    return {k: v for k, v in meta.items() if k not in _KNOWN}


def parse_current_task(text: str) -> tuple[CurrentTask, str]:
    """Parse `current-task.md` into (document, body). Unknown fields survive."""
    meta, body = parse_frontmatter(text)
    where = "current-task.md"
    extra = _common(meta, where=where, require_source=False)
    if not isinstance(meta, dict):
        raise ContextSchemaError(f"{where}: frontmatter must be a mapping")
    task = _require_str(meta, "task", where=where, max_chars=MAX_TASK_CHARS)
    if not task:
        raise ContextSchemaError(f"{where}.task: required")
    return (
        CurrentTask(
            task=task,
            acceptance_criteria=tuple(
                _require_str_list(meta, "acceptance_criteria", where=where, limit=MAX_CRITERIA)
            ),
            changed_files=tuple(
                _require_str_list(meta, "changed_files", where=where, limit=MAX_FILES)
            ),
            evidence=tuple(
                _require_str_list(meta, "evidence", where=where, limit=MAX_EVIDENCE)
            ),
            blockers=tuple(
                _require_str_list(meta, "blockers", where=where, limit=MAX_BLOCKERS)
            ),
            next_action=_require_str(meta, "next_action", where=where, max_chars=MAX_TEXT_CHARS),
            source_runtime=meta.get("source_runtime", "native") or "native",
            extra=extra,
        ),
        body,
    )


def parse_handoff(text: str) -> tuple[Handoff, str]:
    """Parse `handoff.md` into (document, body). `source_runtime` is required."""
    meta, body = parse_frontmatter(text)
    where = "handoff.md"
    extra = _common(meta, where=where, require_source=True)
    if not isinstance(meta, dict):
        raise ContextSchemaError(f"{where}: frontmatter must be a mapping")
    for key in ("garuda_session_id", "native_session_id"):
        if key in meta and not isinstance(meta[key], str):
            raise ContextSchemaError(f"{where}.{key}: must be a string")
    task = _require_str(meta, "task", where=where, max_chars=MAX_TASK_CHARS)
    if not task:
        raise ContextSchemaError(f"{where}.task: required")
    return (
        Handoff(
            task=task,
            source_runtime=str(meta.get("source_runtime")),
            acceptance_criteria=tuple(
                _require_str_list(meta, "acceptance_criteria", where=where, limit=MAX_CRITERIA)
            ),
            changed_files=tuple(
                _require_str_list(meta, "changed_files", where=where, limit=MAX_FILES)
            ),
            evidence=tuple(
                _require_str_list(meta, "evidence", where=where, limit=MAX_EVIDENCE)
            ),
            blockers=tuple(
                _require_str_list(meta, "blockers", where=where, limit=MAX_BLOCKERS)
            ),
            next_action=_require_str(meta, "next_action", where=where, max_chars=MAX_TEXT_CHARS),
            garuda_session_id=meta.get("garuda_session_id", "") or "",
            native_session_id=meta.get("native_session_id", "") or "",
            redacted=bool(meta.get("redacted", False)),
            extra=extra,
        ),
        body,
    )


def render(frontmatter: dict[str, Any], body: str = "") -> str:
    """Render a document deterministically: frontmatter first, body after."""
    text = yaml.safe_dump(dict(frontmatter), sort_keys=True, allow_unicode=True)
    return f"---\n{text}---\n{body.strip()}\n" if body.strip() else f"---\n{text}---\n"
