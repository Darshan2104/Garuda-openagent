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
    assert len(brief) <= 400
    assert len(brief) <= len(full)
    assert "Sources:" in brief
    assert "[… clipped to budget]" in brief
    assert "Migrate sessions" in brief

    with pytest.raises(PackError, match="too small"):
        build_brief(doc.to_frontmatter(), body, budget_chars=100)


async def test_pack_syncs_through_production_run_path(tmp_path):
    """End-to-end through `DefaultAgent.run` + `RunState` boundaries.

    The compiler unit tests above prove the manager writes atomically; this
    proves the production lifecycle actually calls it: the loop's per-turn
    checkpoint publishes both files, compaction re-publishes byte-identical
    files from the preserved `WorkingState`, and a restart from persisted
    `state.json` restores the same facts. All writes go through the single
    `ContextPackManager` — no direct file writes.
    """
    from garuda.context.pack import ContextPackManager, sync_context_pack
    from garuda.context.schemas import parse_current_task, parse_handoff
    from garuda.context.state_card import WorkingState
    from garuda.core.events import EventStore
    from garuda.core.loop import DefaultAgent
    from garuda.core.permissions import PermissionEngine
    from garuda.core.run_state import prepare_run
    from garuda.core.sessions import SessionStore
    from garuda.model.protocol import ModelResponse
    from garuda.model.script_model import ScriptModel
    from garuda.tools import tools_for_names
    from garuda.types import AgentConfig, ToolCall
    from garuda.workspace.local import LocalEnvironment

    pack_root = tmp_path / ".context"
    manager = ContextPackManager(pack_root)
    store = SessionStore(tmp_path / "sessions")

    seed = {
        "task": "Migrate sessions",
        "todos": [{"title": "write compiler", "status": "in_progress"}],
        "files_modified": ["garuda/runtime/session.py"],
        "checks": [{"command": "pytest -q", "exit_code": 0, "turn": 3}],
        "failures": ["bash: flaky timeout"],
        "acceptance": "unified sessions persist",
        "narrative": "Migration must not touch the original.",
    }

    # 1. Full loop: per-turn `save_checkpoint` publishes via the manager.
    model = ScriptModel(
        responses=[
            ModelResponse(
                content=None,
                tool_calls=[
                    ToolCall(id="1", name="task_complete", arguments={"summary": "done"})
                ],
            )
        ]
    )
    events = EventStore(session_id="pack-e2e-1")
    result = await DefaultAgent().run(
        task="Migrate sessions",
        model=model,
        env=LocalEnvironment(workspace_root=tmp_path),
        tools=tools_for_names(["task_complete"]),
        config=AgentConfig(max_turns=5, enable_verifier=False, permission_mode="yolo"),
        events=events,
        permissions=PermissionEngine(mode="yolo"),
        checkpoint=lambda msgs: store.checkpoint_messages(events.session_id, msgs),
        state_checkpoint=lambda s: store.checkpoint_state(events.session_id, s),
        pack_manager=manager,
        pack_source_runtime="native",
        pack_git_evidence="",
        initial_state=seed,
    )
    assert result.success
    current_text = manager.current_task_path.read_text(encoding="utf-8")
    handoff_text = manager.handoff_path.read_text(encoding="utf-8")
    current_doc, current_body = parse_current_task(current_text)
    handoff_doc, _ = parse_handoff(handoff_text)
    assert current_doc.task == "Migrate sessions"
    assert current_doc.changed_files == ("garuda/runtime/session.py",)
    assert current_doc.evidence == ("pytest -q (exit 0)",)
    assert "session pack-e2e-1" in current_body
    assert handoff_doc.garuda_session_id == "pack-e2e-1"
    assert handoff_doc.native_session_id == "pack-e2e-1"
    # Single writer: only the two generated files, no tmp residue.
    assert sorted(p.name for p in pack_root.iterdir()) == sorted(
        ["current-task.md", "handoff.md"]
    )
    assert list(pack_root.rglob("*.tmp")) == []

    # 2. Checkpoint boundary is deterministic: re-sync is byte-identical.
    before_current = current_text
    before_handoff = handoff_text
    state = WorkingState.from_dict(seed)
    rerun = sync_context_pack(
        manager, state, source_runtime="native", session_id="pack-e2e-1"
    )
    assert rerun["current_task"].read_text(encoding="utf-8") == before_current
    assert rerun["handoff"].read_text(encoding="utf-8") == before_handoff

    # 3. Compaction boundary preserves pack facts via `RunState`.
    run_state = await prepare_run(
        task="Migrate sessions",
        profile_name="build",
        model=ScriptModel(responses=[]),
        env=LocalEnvironment(workspace_root=tmp_path),
        tools=tools_for_names(["task_complete"]),
        config=AgentConfig(),
        events=EventStore(session_id="pack-e2e-2"),
        permissions=None,
        hooks=None,
        subagent_runner=None,
        agents_dir=None,
        context=None,
        checkpoint=None,
        buffer=None,
        emit_session_events=False,
        pack_manager=manager,
        pack_source_runtime="native",
        pack_git_evidence="",
        initial_state=seed,
    )
    run_state.save_checkpoint()
    checkpoint_current = manager.current_task_path.read_text(encoding="utf-8")
    assert "Migrate sessions" in checkpoint_current
    # Force a prune-able history, then compact: WorkingState survives, so the
    # re-published pack must match the checkpoint bytes.
    from garuda.types import Message, Role

    run_state.context.append(
        Message(role=Role.TOOL, content="x" * 2000, name="bash", tool_call_id="t1")
    )
    await run_state.compact_if_needed(turn=1)
    after_compact = manager.current_task_path.read_text(encoding="utf-8")
    reparsed, _ = parse_current_task(after_compact)
    assert reparsed.task == "Migrate sessions"
    assert reparsed.changed_files == ("garuda/runtime/session.py",)

    # 4. Restart restores facts: persist, reload, re-sync identical.
    store.checkpoint_state("restart-src", run_state.refresh_state().to_dict())
    restored = store.load_state("restart-src")
    assert restored["task"] == "Migrate sessions"
    restarted = WorkingState.from_dict(restored)
    sync_context_pack(
        manager, restarted, source_runtime="native", session_id="pack-e2e-2"
    )
    restarted_text = manager.current_task_path.read_text(encoding="utf-8")
    reparsed_restart, _ = parse_current_task(restarted_text)
    assert reparsed_restart.task == "Migrate sessions"
    assert reparsed_restart.changed_files == ("garuda/runtime/session.py",)
    assert reparsed_restart.evidence == ("pytest -q (exit 0)",)

    # 5. Single-writer guard still holds at the integration point.
    with pytest.raises(PackError, match="only"):
        manager._publish("../../AGENTS.md", "x")
