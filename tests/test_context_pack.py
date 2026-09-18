"""Pack compiler tests for issue #17 (P0.9).

Deterministic rendering goldens, crash recovery, the durable-file guard, and
budgeted briefs that keep provenance.
"""

import pytest

from garuda.context.pack import (
    ContextPackManager,
    PackError,
    build_brief,
    compile_current_task,
    compile_handoff,
)
from garuda.context.schemas import parse_current_task, parse_handoff, render
from garuda.context.state_card import WorkingState


def _state() -> WorkingState:
    state = WorkingState(
        task="Migrate sessions",
        todos=[
            {"title": "write schema", "status": "done"},
            {"title": "write compiler", "status": "in_progress"},
        ],
        files_modified=["garuda/runtime/session.py"],
        acceptance="unified sessions persist\nlegacy sessions resume",
        narrative="Migration must not touch the original.",
    )
    state.note_check("pytest -q", 0, turn=3)
    state.note_failure("bash", "flaky timeout")
    return state


def test_compile_is_deterministic_and_cites_sources():
    first = compile_current_task(_state(), source_runtime="native", session_id="s1")
    second = compile_current_task(_state(), source_runtime="native", session_id="s1")
    assert render(first[0].to_frontmatter(), first[1]) == render(
        second[0].to_frontmatter(), second[1]
    )
    doc, body = first
    assert doc.next_action == "write compiler"
    assert doc.evidence == ("pytest -q (exit 0)",)
    assert doc.blockers == ("bash: flaky timeout",)
    assert "Sources: state card, session s1, git status" in body


def test_handoff_carries_session_identity():
    doc, body = compile_handoff(
        _state(),
        source_runtime="native",
        session_id="g1",
        native_session_id="n1",
        redacted=True,
    )
    assert doc.garuda_session_id == "g1"
    assert doc.native_session_id == "n1"
    assert doc.redacted is True
    assert doc.task == "Migrate sessions"
    parsed, _ = parse_handoff(render(doc.to_frontmatter(), body))
    assert parsed == doc


def test_compiled_current_task_parses_as_schema():
    doc, body = compile_current_task(_state())
    parsed, _ = parse_current_task(render(doc.to_frontmatter(), body))
    assert parsed == doc


def test_writer_is_atomic_and_guarded(tmp_path):
    manager = ContextPackManager(tmp_path / ".context")
    doc, body = compile_current_task(_state())
    target = manager.write_current_task(doc, body)
    assert target.read_text(encoding="utf-8") == render(doc.to_frontmatter(), body)
    assert list(tmp_path.rglob("*.tmp")) == []

    with pytest.raises(PackError, match="only"):
        manager._publish("../../AGENTS.md", "x")
    with pytest.raises(PackError, match="only"):
        manager._publish("decisions.md", "x")


def test_crash_mid_write_leaves_no_partial_file(tmp_path, monkeypatch):
    import garuda.context.pack as pack_mod

    manager = ContextPackManager(tmp_path / ".context")
    doc, body = compile_current_task(_state())
    manager.write_current_task(doc, body)
    before = manager.current_task_path.read_text(encoding="utf-8")

    def _boom(*args, **kwargs):
        raise OSError("disk is gone")

    monkeypatch.setattr(pack_mod.os, "replace", _boom)
    with pytest.raises(OSError):
        manager.write_current_task(doc, body + "more")
    assert manager.current_task_path.read_text(encoding="utf-8") == before


def test_brief_respects_budget_and_keeps_sources():
    doc, body = compile_current_task(
        _state(), git_evidence="M garuda/x.py\n" * 200
    )
    full = render(doc.to_frontmatter(), body)
    brief = build_brief(doc.to_frontmatter(), body, budget_chars=400)
    assert len(brief) <= len(full)
    assert "Sources:" in brief
    assert "[… clipped to budget]" in brief
    assert "Migrate sessions" in brief
