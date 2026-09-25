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

import logging
import os
import subprocess
from pathlib import Path

import yaml

from garuda.context import schemas
from garuda.context.redact import assert_safe_for_switch, redact_pack
from garuda.context.schemas import CurrentTask, Handoff
from garuda.context.state_card import WorkingState

CURRENT_TASK_NAME = "current-task.md"
HANDOFF_NAME = "handoff.md"
_GENERATED = frozenset({CURRENT_TASK_NAME, HANDOFF_NAME})

MAX_GIT_CHARS = 2_000

logger = logging.getLogger(__name__)


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
    paragraphs = [p for p in body.split("\n\n") if p.strip()]
    sources = ""
    if paragraphs and paragraphs[-1].startswith("Sources:"):
        sources = paragraphs.pop()

    def _render(parts: list[str]) -> str:
        text = "\n\n".join(parts)
        return head_text + text.strip() + "\n"

    full_parts = [*paragraphs, *([sources] if sources else [])]
    full = _render(full_parts)
    if len(full) <= budget_chars:
        return full

    marker = "[… clipped to budget]"
    required = [marker, *([sources] if sources else [])]
    minimum = _render(required)
    if len(minimum) > budget_chars:
        raise PackError(
            "brief budget is too small for required frontmatter and provenance "
            f"({budget_chars} < {len(minimum)})"
        )

    kept: list[str] = []
    for paragraph in paragraphs:
        candidate = _render([*kept, paragraph, *required])
        if len(candidate) > budget_chars:
            break
        kept.append(paragraph)
    return _render([*kept, *required])


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
        """Redact *every* persisted string, then rebuild from the scrubbed map.

        Fixed vs the first redaction cut: the old code validated the redacted
        frontmatter but rebuilt the dataclass from the original scalar/extra
        fields, so secrets in `task`, session IDs, or nested unknown-frontmatter
        survived. Here every scalar (`task`, `next_action`, `source_runtime`,
        session IDs), every list field, every `extra` key/value (recursively),
        and the body are scrubbed via `redact_pack`; the document is rebuilt
        from that scrubbed mapping, never from the original object.
        """
        # Start from the full frontmatter so `extra` (unknown fields) is included.
        frontmatter = dict(doc.to_frontmatter())
        scrubbed_front, scrubbed_body, findings = redact_pack(frontmatter, body)
        # Re-validate the redacted text; changed_files re-checked post-redaction.
        assert_safe_for_switch(
            scrubbed_front,
            scrubbed_body,
            changed_files=tuple(scrubbed_front.get("changed_files", [])),
        )
        # Rebuild from the scrubbed mapping — the original `doc` is discarded.
        list_fields: dict[str, tuple[str, ...]] = {}
        for name in ("acceptance_criteria", "changed_files", "evidence", "blockers"):
            raw = scrubbed_front.get(name, [])
            list_fields[name] = tuple(str(v) for v in raw) if isinstance(raw, list) else ()
        extra = {
            k: v for k, v in scrubbed_front.items() if k not in {
                "version", "source_runtime", "task", "acceptance_criteria",
                "changed_files", "evidence", "blockers", "next_action",
                "garuda_session_id", "native_session_id", "redacted",
            }
        }
        redacted = bool(scrubbed_front.get("redacted", False))
        if findings and redacted_flag:
            redacted = True
        if isinstance(doc, Handoff):
            doc = Handoff(
                task=str(scrubbed_front.get("task", "")),
                source_runtime=str(scrubbed_front.get("source_runtime", "native")),
                acceptance_criteria=list_fields["acceptance_criteria"],
                changed_files=list_fields["changed_files"],
                evidence=list_fields["evidence"],
                blockers=list_fields["blockers"],
                next_action=str(scrubbed_front.get("next_action", "")),
                garuda_session_id=str(scrubbed_front.get("garuda_session_id", "")),
                native_session_id=str(scrubbed_front.get("native_session_id", "")),
                redacted=redacted,
                extra=extra,
            )
        else:
            doc = CurrentTask(
                task=str(scrubbed_front.get("task", "")),
                acceptance_criteria=list_fields["acceptance_criteria"],
                changed_files=list_fields["changed_files"],
                evidence=list_fields["evidence"],
                blockers=list_fields["blockers"],
                next_action=str(scrubbed_front.get("next_action", "")),
                source_runtime=str(scrubbed_front.get("source_runtime", "native")),
                extra=extra,
            )
            if findings and redacted_flag:
                # CurrentTask has no redacted flag; surface via body note below.
                pass
        # Keep list-level findings for the body note (kinds only; counts merged).
        if findings:
            kinds = sorted({f.kind for f in findings})
            scrubbed_body = (
                scrubbed_body.rstrip()
                + f"\n\nRedaction: {len(findings)} secret(s) redacted ({', '.join(kinds)}).\n"
            )
        return doc, scrubbed_body


def collect_git_evidence(workspace_root: str | Path | None) -> str:
    """Best-effort git evidence for the pack's provenance section.

    Runs `git status --short` (plus a one-line HEAD) inside `workspace_root`.
    Never raises: outside a repo, without git, or on any failure the pack
    simply cites the state card and session instead. Truncated to
    `MAX_GIT_CHARS` so an enormous dirty tree cannot bloat the pack.
    """
    if not workspace_root:
        return ""
    try:
        root = Path(workspace_root)
        if not root.is_dir():
            return ""
        status = subprocess.run(
            ["git", "status", "--short"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if status.returncode != 0:
            return ""
        head = subprocess.run(
            ["git", "log", "-1", "--oneline"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=5,
        )
        parts: list[str] = []
        if head.returncode == 0 and head.stdout.strip():
            parts.append(f"HEAD {head.stdout.strip()}")
        if status.stdout.strip():
            parts.append(status.stdout.strip())
        return "\n".join(parts)[:MAX_GIT_CHARS]
    except Exception:
        logger.debug("Git evidence collection failed", exc_info=True)
        return ""


def sync_context_pack(
    manager: ContextPackManager,
    state: WorkingState,
    *,
    source_runtime: str = "native",
    session_id: str = "",
    native_session_id: str = "",
    git_evidence: str = "",
    redacted: bool = False,
) -> dict[str, Path]:
    """Compile `WorkingState` and publish both generated files via `manager`.

    The single-writer rule lives here: every production write goes through
    `ContextPackManager._publish`, which refuses any target outside the two
    generated filenames and publishes atomically. Callers invoke this at the
    documented checkpoint/compaction boundaries; recompilation is deterministic,
    so re-syncing after compaction or restart reproduces byte-identical files
    exactly when the underlying facts survived — which is the preservation
    property the integration tests assert.
    """
    current_doc, current_body = compile_current_task(
        state,
        source_runtime=source_runtime,
        session_id=session_id,
        git_evidence=git_evidence,
    )
    handoff_doc, handoff_body = compile_handoff(
        state,
        source_runtime=source_runtime,
        session_id=session_id,
        native_session_id=native_session_id or session_id,
        git_evidence=git_evidence,
        redacted=redacted,
    )
    return {
        "current_task": manager.write_current_task(current_doc, current_body),
        "handoff": manager.write_handoff(handoff_doc, handoff_body),
    }
