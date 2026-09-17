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
    recover,
    report_to_dict,
)


def _begin(store: SessionStore, session_id: str = "s1") -> None:
    store.begin(session_id, task="t", model="m", agent="a", workspace="w")


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
