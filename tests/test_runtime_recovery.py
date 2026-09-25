"""Recovery tests for issue #29 (P0.19).

Fault injection at every boundary and restart path: crash mid-run, failed and
prepared switches, orphan children, ambiguous terminals, and the guarantee
that a bare exit never becomes a success claim.
"""

import pytest

from garuda.core.sessions import SessionStore
from garuda.runtime.events import RuntimeEvent, RuntimeEventKind
from garuda.runtime.recovery import (
    RecoveryError,
    RestartState,
    audit_terminal,
    classify,
    reap_orphans,
    record_cancel,
    record_child,
    recover,
    report_to_dict,
)


def _begin(store: SessionStore, session_id: str = "s1") -> None:
    store.begin(session_id, task="t", model="m", agent="a", workspace="w")
    store.checkpoint_messages(session_id, [])


def _event(terminal: bool, seq: int) -> RuntimeEvent:
    return RuntimeEvent(
        kind=RuntimeEventKind.LIFECYCLE,
        session_id="s",
        turn=1,
        seq=seq,
        payload={"state": "cancelled" if terminal else "started"},
    )


def test_crash_mid_run_resumes_from_checkpoints(tmp_path):
    store = SessionStore(tmp_path)
    _begin(store)
    store.ensure_unified("s1")
    report = recover(store, "s1")
    assert report.state is RestartState.RESUMABLE
    assert report.resume_session_id == "s1"
    assert report.success_claim is False
    assert store.load_unified("s1").active.runtime_id == "native"


def test_failed_switch_keeps_source_resumable(tmp_path):
    store = SessionStore(tmp_path)
    _begin(store)
    store.ensure_unified("s1")
    store.record_handoff("s1", state="failed", attempts=1)
    report = recover(store, "s1")
    assert report.state is RestartState.RESUMABLE
    assert report.resume_session_id == "s1"


def test_prepared_switch_rolls_back_before_resume(tmp_path):
    store = SessionStore(tmp_path)
    _begin(store)
    store.ensure_unified("s1")
    store.record_handoff("s1", state="prepared", attempts=1, target_runtime="codex")
    report = recover(store, "s1")
    assert report.state is RestartState.ROLLED_BACK
    assert store.load_unified("s1").handoff["state"] == "failed"
    assert store.load_unified("s1").segments[0].runtime_id == "native"


def test_orphans_reaped_and_verified():
    live = {4242}

    def is_alive(pid: int) -> bool:
        return pid in live

    def reap(pid: int) -> None:
        live.discard(pid)

    assert reap_orphans([4242, 9999], is_alive=is_alive, reap=reap) == (4242,)
    with pytest.raises(RecoveryError, match="survived reaping"):
        reap_orphans([4242], is_alive=lambda pid: True, reap=lambda pid: None)
    states = iter((True, None))
    with pytest.raises(RecoveryError, match="indeterminate liveness after reaping"):
        reap_orphans(
            [4242],
            is_alive=lambda _pid: next(states),
            reap=lambda pid: None,
        )
    for invalid in (0, -1, "4242", True):
        with pytest.raises(RecoveryError, match="invalid persisted child pid"):
            reap_orphans([invalid], is_alive=lambda _pid: False)


def test_recovery_uses_only_persisted_validated_child_identity(tmp_path):
    store = SessionStore(tmp_path)
    _begin(store)
    store.ensure_unified("s1")
    record_child(store, "s1", runtime_id="native", pid=4242)
    live = {4242}
    report = recover(
        store,
        "s1",
        is_alive=lambda pid: pid in live,
        reap=lambda pid: live.discard(pid),
    )
    assert report.reaped_pids == (4242,)
    store.update_meta(
        "s1",
        {
            "runtime_children": [
                {
                    "session_id": "other",
                    "runtime_id": "native",
                    "pid": 4242,
                    "process_group": 4242,
                }
            ]
        },
    )
    with pytest.raises(RecoveryError, match="mismatched session identity"):
        recover(store, "s1")


def test_ambiguous_terminal_and_unreadable_refuse(tmp_path):
    assert audit_terminal([_event(False, 0), _event(True, 1)]) is True
    assert audit_terminal([_event(True, 0), _event(True, 1)]) is False
    assert audit_terminal([_event(True, 0), _event(False, 1)]) is False
    assert audit_terminal([]) is True

    store = SessionStore(tmp_path)
    with pytest.raises(RecoveryError, match="unreadable"):
        classify(store, "missing")
    _begin(store, "bad")
    store.update_meta("bad", {"session_id": ""})
    with pytest.raises(RecoveryError, match="no identity"):
        classify(store, "bad")


def test_classify_requires_checkpoint_identity_and_acp_authority(tmp_path):
    store = SessionStore(tmp_path)
    store.begin("missing-checkpoint", task="t", model="m", agent="a", workspace="w")
    store.ensure_unified("missing-checkpoint")
    with pytest.raises(RecoveryError, match="message checkpoint"):
        classify(store, "missing-checkpoint")

    _begin(store, "bad-native")
    store.ensure_unified("bad-native")
    meta = store.load_meta("bad-native")
    meta["runtime_segments"][0]["native_session_id"] = "other"
    store.update_meta("bad-native", meta)
    with pytest.raises(RecoveryError, match="identity does not match"):
        classify(store, "bad-native")

    _begin(store, "bad-acp")
    store.ensure_unified("bad-acp")
    from garuda.runtime.session import RuntimeSegment

    store.attach_runtime_segment(
        "bad-acp",
        RuntimeSegment(runtime_id="acp", kind="acp", native_session_id="agent", capabilities=frozenset()),
    )
    with pytest.raises(RecoveryError, match="authority snapshot"):
        classify(store, "bad-acp")


def test_cancel_boundaries_recorded(tmp_path):
    store = SessionStore(tmp_path)
    _begin(store)
    for boundary in ("turn", "switch", "process"):
        record_cancel(store, "s1", boundary=boundary, reason="test")
        assert store.load_meta("s1")["cancellation"]["boundary"] == boundary
    with pytest.raises(RecoveryError, match="unknown cancellation boundary"):
        record_cancel(store, "s1", boundary="mid-thought")


def test_exit_is_never_success(tmp_path):
    store = SessionStore(tmp_path)
    _begin(store)
    store.update_meta("s1", {"status": "failed"})
    report = recover(store, "s1")
    assert report_to_dict(report)["success_claim"] is False
    assert report_to_dict(report)["state"] == "resumable"
    assert store.load_meta("s1")["status"] == "failed"


def test_indeterminate_liveness_refuses_without_operator_action(tmp_path, monkeypatch):
    import os as _os

    import garuda.runtime.recovery as recovery_mod

    store = SessionStore(tmp_path)
    _begin(store)
    store.ensure_unified("s1")
    record_child(store, "s1", runtime_id="native", pid=1234)
    # PermissionError (another owner's process) is unknown, not dead.
    monkeypatch.setattr(_os, "kill", lambda pid, sig: (_ for _ in ()).throw(PermissionError()))
    assert recovery_mod._process_live(1234) is None
    with pytest.raises(RecoveryError, match="indeterminate liveness"):
        recover(store, "s1")
    # ...and an injected unknown verdict refuses the same way.
    monkeypatch.setattr(_os, "kill", lambda pid, sig: None)
    assert recovery_mod._process_live(1234) is True
    with pytest.raises(RecoveryError, match="indeterminate liveness"):
        recover(store, "s1", is_alive=lambda pid: None)


def test_malformed_or_ambiguous_trail_blocks_recovery(tmp_path):
    from garuda.runtime.recovery import audit_core_trail

    store = SessionStore(tmp_path)
    _begin(store)
    events_path = store.events_path("s1")
    assert audit_core_trail(events_path) is True  # no trail yet
    events_path.parent.mkdir(parents=True, exist_ok=True)
    events_path.write_text(
        '{"type": "session_end"}\n{"type": "session_end"}\n', encoding="utf-8"
    )
    assert audit_core_trail(events_path) is False
    with pytest.raises(RecoveryError, match="ambiguous terminal"):
        recover(store, "s1")
    events_path.write_text('{"type": "session_end"}\n{broken\n', encoding="utf-8")
    assert audit_core_trail(events_path) is False
    with pytest.raises(RecoveryError, match="ambiguous terminal"):
        recover(store, "s1")
    # Post-terminal telemetry is bookkeeping, not ambiguity...
    events_path.write_text(
        '{"type": "session_end"}\n{"type": "turn_metrics"}\n{"type": "budget"}\n',
        encoding="utf-8",
    )
    assert audit_core_trail(events_path) is True
    # ...but substantive traffic after the terminal is.
    events_path.write_text(
        '{"type": "session_end"}\n{"type": "user_message"}\n', encoding="utf-8"
    )
    assert audit_core_trail(events_path) is False
    with pytest.raises(RecoveryError, match="ambiguous terminal"):
        recover(store, "s1")


async def test_resume_classifies_through_the_production_path(tmp_path, monkeypatch):
    """`run_agent_task --resume` classifies first: prepared switches roll
    back and resume; ambiguous trails refuse."""
    monkeypatch.setenv("GARUDA_LEASES_DIR", str(tmp_path / "leases"))
    from garuda.core.events import EventStore
    from garuda.core.loop import DefaultAgent
    from garuda.core.permissions import PermissionEngine
    from garuda.interfaces.runner import run_agent_task
    from garuda.model.protocol import ModelResponse
    from garuda.model.script_model import ScriptModel
    from garuda.tools import tools_for_names
    from garuda.types import AgentConfig, ToolCall

    def _script(summary="ok"):
        return ScriptModel(
            responses=[
                ModelResponse(
                    content=None,
                    tool_calls=[
                        ToolCall(id="1", name="task_complete", arguments={"summary": summary})
                    ],
                )
            ]
        )

    def _run(task, session_id, **kwargs):
        return run_agent_task(
            task=task,
            model=_script(),
            agent=DefaultAgent(),
            tools=tools_for_names(["task_complete"]),
            config=AgentConfig(max_turns=5, enable_verifier=False, permission_mode="yolo"),
            permissions=PermissionEngine(mode="yolo"),
            workspace=str(tmp_path / "ws"),
            events=EventStore(session_id=session_id),
            store=SessionStore(tmp_path / "sessions"),
            **kwargs,
        )

    (tmp_path / "ws").mkdir(exist_ok=True)
    first = await _run("first task", "rec-first")
    assert first.success
    store = SessionStore(tmp_path / "sessions")
    store.record_handoff("rec-first", state="prepared", attempts=1)
    second = await _run("second task", "rec-second", resume="rec-first")
    assert second.success
    assert store.load_unified("rec-first").handoff["state"] == "failed"

    events_path = store.events_path("rec-first")
    with events_path.open("a", encoding="utf-8") as handle:
        handle.write('{"type": "session_end"}\n')
    with pytest.raises(RecoveryError, match="ambiguous terminal"):
        await _run("third task", "rec-third", resume="rec-first")


async def test_native_resume_refuses_ambiguous_trail(tmp_path):
    from garuda.core.loop import DefaultAgent
    from garuda.core.permissions import PermissionEngine
    from garuda.model.script_model import ScriptModel
    from garuda.runtime.native import NativeGarudaRuntime
    from garuda.runtime.protocol import RuntimeStartError
    from garuda.tools import tools_for_names
    from garuda.types import AgentConfig

    store = SessionStore(tmp_path)
    store.begin("amb", task="t", model="m", agent="a", workspace="w")
    store.checkpoint_messages("amb", [])
    events_path = store.events_path("amb")
    events_path.parent.mkdir(parents=True, exist_ok=True)
    events_path.write_text(
        '{"type": "session_end"}\n{"type": "session_end"}\n', encoding="utf-8"
    )
    runtime = NativeGarudaRuntime(
        agent=DefaultAgent(),
        model=ScriptModel(responses=[]),
        tools=tools_for_names(["task_complete"]),
        config=AgentConfig(),
        permissions=PermissionEngine(mode="yolo"),
        store=store,
    )
    with pytest.raises(RuntimeStartError, match="ambiguous terminal"):
        await runtime.resume(native_session_id="amb")


async def test_handoff_cancel_records_the_real_switch_boundary(tmp_path):
    from garuda.runtime.fake import FakeRuntime, FakeScenario
    from garuda.runtime.handoff import HandoffTransaction

    store = SessionStore(tmp_path)
    _begin(store, "switch-cancel")
    source = FakeRuntime(FakeScenario.SUCCESS, runtime_id="native")
    await source.start(task="t", session_id="switch-cancel")
    tx = HandoffTransaction(session_id="switch-cancel", store=store)
    await tx.begin(source, checkpoint=lambda: None, capture=lambda: {}, generate=lambda: None)
    await tx.cancel(source, reason="operator stopped handoff")
    assert store.load_meta("switch-cancel")["cancellation"] == {
        "boundary": "switch",
        "reason": "operator stopped handoff",
    }
