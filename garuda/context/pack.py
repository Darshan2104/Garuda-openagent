"""Single-writer context pack compiler (P0.9, issue #17).

`ContextPackManager` is the only writer of the generated context files
(`current-task.md`, `handoff.md`). It compiles them deterministically from
sources the harness already holds exactly — the `WorkingState` card, the
unified session record, git evidence, and verifier check records — so every
fact in the pack cites a source path and recompilation is byte-identical
(which is what lets compaction preserve pack facts: the inputs are the facts).

Durable repository files are never touched: writes are refused unless the
target is one of the two generated filenames inside the manager's root, and
every write is atomic (temp file + replace), so a crash cannot leave a half
written pack behind.
"""

from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path

import yaml

from garuda.context import schemas
from garuda.context.redact import (
    assert_safe_for_switch,
    redact_pack,
    redact_string_list,
)
from garuda.context.schemas import CurrentTask, Handoff
from garuda.context.state_card import WorkingState

CURRENT_TASK_NAME = "current-task.md"
HANDOFF_NAME = "handoff.md"
_GENERATED = frozenset({CURRENT_TASK_NAME, HANDOFF_NAME})

MAX_GIT_CHARS = 2_000


class PackError(ValueError):
    """The pack writer was asked to write outside its mandate. Fail-closed."""


def _incomplete_todos(state: WorkingState) -> list[str]:
    return [
        str(t.get("title", t.get("text", "")))
        for t in state.todos
        if isinstance(t, dict) and t.get("status") not in ("done", "completed")
    ]


def compile_current_task(
    state: WorkingState,
    *,
    source_runtime: str = "native",
    session_id: str = "",
    git_evidence: str = "",
) -> tuple[CurrentTask, str]:
    """Build the current-task document and its body from harness-held facts."""
    todos = _incomplete_todos(state)
    criteria = [line.strip() for line in state.acceptance.splitlines() if line.strip()]
    doc = CurrentTask(
        task=state.task,
        acceptance_criteria=tuple(criteria[: schemas.MAX_CRITERIA]),
        changed_files=tuple(state.files_modified[: schemas.MAX_FILES]),
        evidence=tuple(
            f"{c.command} (exit {c.exit_code})" for c in state.checks[: schemas.MAX_EVIDENCE]
        ),
        blockers=tuple(state.failures[: schemas.MAX_BLOCKERS]),
        next_action=todos[0] if todos else "",
        source_runtime=source_runtime or "native",
    )
    sections = [f"Task: {state.task}"]
    if state.narrative.strip():
        sections.append(f"Findings:\n{state.narrative.strip()}")
    if git_evidence.strip():
        sections.append(f"Git evidence:\n{git_evidence.strip()[:MAX_GIT_CHARS]}")
    sources = ["state card"]
    if session_id:
        sources.append(f"session {session_id}")
    sources.append("git status")
    sections.append(f"Sources: {', '.join(sources)}")
    return doc, "\n\n".join(sections) + "\n"


def compile_handoff(
    state: WorkingState,
    *,
    source_runtime: str,
    session_id: str = "",
    native_session_id: str = "",
    git_evidence: str = "",
    redacted: bool = False,
) -> tuple[Handoff, str]:
    """Build the handoff transfer package. Same facts as the current task, plus
    session identity so the target runtime can correlate the switch."""
    doc, body = compile_current_task(
        state,
        source_runtime=source_runtime,
        session_id=session_id,
        git_evidence=git_evidence,
    )
    return (
        Handoff(
            task=doc.task,
            source_runtime=doc.source_runtime,
            acceptance_criteria=doc.acceptance_criteria,
            changed_files=doc.changed_files,
            evidence=doc.evidence,
            blockers=doc.blockers,
            next_action=doc.next_action,
            garuda_session_id=session_id,
            native_session_id=native_session_id,
            redacted=redacted,
            extra=doc.extra,
        ),
        body,
    )


def build_brief(frontmatter: dict, body: str, *, budget_chars: int) -> str:
    """Clip a compiled document to `budget_chars` keeping task, next action, sources.

    Frontmatter always survives intact (it is the routing minimum); the body is
    clipped with an explicit marker, sources last so provenance is never the
    part that gets cut.
    """
    head = yaml.safe_dump(dict(frontmatter), sort_keys=True, allow_unicode=True)
    head_text = f"---\n{head}---\n"
    allowance = max(0, budget_chars - len(head_text))
    paragraphs = [p for p in body.split("\n\n") if p.strip()]
    sources = ""
    if paragraphs and paragraphs[-1].startswith("Sources:"):
        sources = paragraphs.pop()
    kept: list[str] = []
    used = 0
    for paragraph in paragraphs:
        cost = len(paragraph) + 2
        if used + cost > allowance:
            break
        kept.append(paragraph)
        used += cost
    clipped = len(kept) < len(paragraphs)
    text = "\n\n".join(kept)
    if clipped:
        text += "\n\n[… clipped to budget]"
    if sources:
        text += f"\n\n{sources}"
    return head_text + text.strip() + "\n"


class ContextPackManager:
    """The single writer of generated context files for one workspace."""

    def __init__(self, root: str | Path):
        self._root = Path(root)

    @property
    def current_task_path(self) -> Path:
        return self._root / CURRENT_TASK_NAME

    @property
    def handoff_path(self) -> Path:
        return self._root / HANDOFF_NAME

    def _guard(self, name: str) -> Path:
        if name not in _GENERATED:
            raise PackError(
                f"refusing to write {name!r}: only {sorted(_GENERATED)} are generated files"
            )
        target = (self._root / name).resolve()
        if target.parent != self._root.resolve():
            raise PackError(f"refusing to write outside the context root: {name!r}")
        return target

    def _publish(self, name: str, text: str) -> Path:
        target = self._guard(name)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(f"{target.name}.{os.getpid()}.tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, target)
        return target

    def write_current_task(self, doc: CurrentTask, body: str = "") -> Path:
        """Validate, redact, then publish. Secrets never reach the file; other
        violations (paths, size, reasoning) block with an actionable error."""
        doc, body = self._scrub(doc, body)
        return self._publish(CURRENT_TASK_NAME, schemas.render(doc.to_frontmatter(), body))

    def write_handoff(self, doc: Handoff, body: str = "") -> Path:
        doc, body = self._scrub(doc, body, redacted_flag=True)
        return self._publish(HANDOFF_NAME, schemas.render(doc.to_frontmatter(), body))

    @staticmethod
    def _scrub(doc, body: str, *, redacted_flag: bool = False):
        """Redact list fields and body, then validate the redacted text."""
        findings: list = []
        changes: dict = {}
        for name in ("acceptance_criteria", "changed_files", "evidence", "blockers"):
            cleaned, found = redact_string_list(getattr(doc, name))
            changes[name] = cleaned
            findings.extend(found)
        frontmatter = {**doc.to_frontmatter(), **{k: list(v) for k, v in changes.items()}}
        frontmatter, body, pack_findings = redact_pack(frontmatter, body)
        findings.extend(pack_findings)
        # changed_files redaction could theoretically rewrite a path; re-check.
        assert_safe_for_switch(
            frontmatter, body, changed_files=tuple(frontmatter.get("changed_files", []))
        )
        if findings and redacted_flag:
            changes["redacted"] = True
        doc = replace(doc, **changes)
        if findings:
            kinds = sorted({f.kind for f in findings})
            body = body.rstrip() + f"\n\nRedaction: {len(findings)} secret(s) redacted ({', '.join(kinds)}).\n"
        return doc, body
