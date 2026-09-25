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


def _fake_identity(pid: int) -> str:
    return f"identity-{pid}"


def _record(store, session_id="s1", *, pid=4242, runtime_id="native"):
    """Record a synthetic child with deterministic identities."""
    record_child(store, session_id, runtime_id=runtime_id, pid=pid, identify=_fake_identity)


def _child_only(pid: int):
    """Identify only the synthetic child; its recorded owner reads as gone."""
    return lambda probe: _fake_identity(pid) if probe == pid else None


def test_orphans_reaped_and_verified():
    live = {4242}

    def is_alive(pid: int) -> bool:
        return pid in live

    def reap(pid: int) -> None:
        live.discard(pid)

    assert reap_orphans([4242, 9999], is_alive=is_alive, reap=reap) == (4242,)
    with pytest.raises(RecoveryError, match="survived reaping"):
        reap_orphans([4242], is_alive=lambda pid: True, reap=lambda pid: None, timeout=0.05)
    states = iter((True, None))
    with pytest.raises(RecoveryError, match="indeterminate liveness after reaping"):
        reap_orphans(
            [4242],
            is_alive=lambda _pid: next(states),
            reap=lambda pid: None,
        )
    for invalid in (0, -1, "4242", True):
        with pytest.raises(RecoveryError, match="invalid persisted child pid"):
            reap_orphans([invalid], is_alive=lambda _pid: False, reap=lambda pid: None)
    import os

    for own in {os.getpid(), os.getpgrp()}:
        with pytest.raises(RecoveryError, match="own pid/process group"):
            reap_orphans([own], is_alive=lambda _pid: True, reap=lambda pid: None)


def test_reaping_waits_for_asynchronous_death():
    """SIGKILL delivery is asynchronous: a leader still visible right after the
    signal must be polled until it dies, not reported as a survivor."""
    verdicts = iter((True, True, True, False))
    reaped: list[int] = []
    assert reap_orphans(
        [4242], is_alive=lambda _pid: next(verdicts), reap=reaped.append
    ) == (4242,)
    assert reaped == [4242]


def test_recovery_uses_only_persisted_validated_child_identity(tmp_path):
    store = SessionStore(tmp_path)
    _begin(store)
    store.ensure_unified("s1")
    _record(store)
    live = {4242}
    report = recover(
        store,
        "s1",
        is_alive=lambda pid: pid in live,
        reap=lambda pid: live.discard(pid),
        identify=_child_only(4242),
    )
    assert report.reaped_pids == (4242,)
    # Recovery retires what it reaped, so a second restart never re-probes it.
    assert store.load_meta("s1")["runtime_children"][0]["state"] == "reaped"
    probed: list[int] = []
    recover(store, "s1", is_alive=lambda pid: probed.append(pid) or True)
    assert probed == []

    good = dict(store.load_meta("s1")["runtime_children"][0], state="live")
    for bad, match in (
        ({**good, "session_id": "other"}, "mismatched session identity"),
        ({**good, "runtime_id": "ghost"}, "not bound to a session runtime"),
        ({**good, "process_group": 4243}, "lead its isolated process group"),
        ({**good, "identity": ""}, "no process identity"),
        ({**good, "owner": {"pid": 7}}, "no owner identity"),
    ):
        store.update_meta("s1", {"runtime_children": [bad]})
        with pytest.raises(RecoveryError, match=match):
            recover(store, "s1", is_alive=lambda pid: True, reap=lambda pid: None)


def test_record_child_refuses_unsafe_or_unbound_identities(tmp_path):
    import os

    store = SessionStore(tmp_path)
    _begin(store)
    store.ensure_unified("s1")
    with pytest.raises(RecoveryError, match="own pid/process group"):
        record_child(store, "s1", runtime_id="native", pid=os.getpid(), identify=_fake_identity)
    with pytest.raises(RecoveryError, match="not bound to session"):
        record_child(store, "s1", runtime_id="ghost", pid=4242, identify=_fake_identity)
    with pytest.raises(RecoveryError, match="lead its own isolated process group"):
        record_child(
            store, "s1", runtime_id="native", pid=4242, process_group=4243,
            identify=_fake_identity,
        )
    with pytest.raises(RecoveryError, match="cannot read identity"):
        record_child(store, "s1", runtime_id="native", pid=4242, identify=lambda pid: None)
    assert "runtime_children" not in store.load_meta("s1")


def test_reused_pid_is_retired_without_a_signal(tmp_path):
    store = SessionStore(tmp_path)
    _begin(store)
    store.ensure_unified("s1")
    _record(store)
    signalled: list[int] = []
    report = recover(
        store,
        "s1",
        is_alive=lambda pid: True,
        reap=signalled.append,
        identify=lambda pid: "a-different-program" if pid == 4242 else None,
    )
    assert signalled == []
    assert report.reaped_pids == ()
    assert any("reused" in note for note in report.notes)
    assert store.load_meta("s1")["runtime_children"][0]["state"] == "reused"


def test_dead_child_is_retired_exited(tmp_path):
    store = SessionStore(tmp_path)
    _begin(store)
    store.ensure_unified("s1")
    _record(store)
    report = recover(store, "s1", is_alive=lambda pid: False, reap=lambda pid: None,
                     identify=_child_only(4242))
    assert report.reaped_pids == ()
    assert store.load_meta("s1")["runtime_children"][0]["state"] == "exited"


def test_indeterminate_identity_refuses(tmp_path):
    from garuda.runtime.recovery import ProcessIdentityUnavailable

    store = SessionStore(tmp_path)
    _begin(store)
    store.ensure_unified("s1")
    _record(store)

    def child_indeterminate(pid):
        if pid == 4242:
            raise ProcessIdentityUnavailable("ps unavailable")
        return None

    signalled: list[int] = []
    with pytest.raises(RecoveryError, match="identity is indeterminate"):
        recover(store, "s1", is_alive=lambda pid: True, reap=signalled.append,
                identify=child_indeterminate)

    def owner_indeterminate(pid):
        if pid == 4242:
            return _fake_identity(pid)
        raise ProcessIdentityUnavailable("ps unavailable")

    with pytest.raises(RecoveryError, match="cannot confirm owner process"):
        recover(store, "s1", is_alive=lambda pid: True, reap=signalled.append,
                identify=owner_indeterminate)
    assert signalled == []
    assert store.load_meta("s1")["runtime_children"][0]["state"] == "live"


def test_live_owner_refuses_recovery(tmp_path):
    """Another live Garuda instance still owns the child: never reap it."""
    store = SessionStore(tmp_path)
    _begin(store)
    store.ensure_unified("s1")
    _record(store)  # owner identity = _fake_identity(os.getpid())
    signalled: list[int] = []
    with pytest.raises(RecoveryError, match="belongs to live Garuda process"):
        recover(store, "s1", is_alive=lambda pid: True, reap=signalled.append,
                identify=_fake_identity)
    assert signalled == []
    # A recycled owner PID (different identity) is not the owner.
    report = recover(
        store, "s1", is_alive=lambda pid: False, reap=signalled.append,
        identify=lambda pid: _fake_identity(pid) if pid == 4242 else "someone-else",
    )
    assert report.state is RestartState.RESUMABLE


def test_live_lease_holder_refuses_recovery(tmp_path):
    from garuda.workspace.lease import LeaseStore

    store = SessionStore(tmp_path / "sessions")
    _begin(store)
    store.ensure_unified("s1")
    store.record_handoff("s1", state="prepared", attempts=1)
    leases = LeaseStore(tmp_path / "leases")
    leases.acquire(tmp_path / "elsewhere", "s1", mode="mutating")
    with pytest.raises(RecoveryError, match="live mutating workspace lease"):
        recover(store, "s1", leases=leases)
    # Refused before anything is touched: the prepared switch is not rolled back.
    assert store.load_unified("s1").handoff["state"] == "prepared"
    leases.release(tmp_path / "elsewhere", "s1")
    assert recover(store, "s1", leases=leases).state is RestartState.ROLLED_BACK

    (tmp_path / "leases" / "corrupt.json").write_text("{broken", encoding="utf-8")
    with pytest.raises(RecoveryError, match="cannot audit workspace leases"):
        recover(store, "s1", leases=leases)


def _spawn_orphan_group() -> int:
    """Start a real process-group leader whose parent has already exited.

    The intermediate parent launches the sleeper in its own session and exits,
    so the sleeper is reparented exactly like the child of a crashed Garuda.
    """
    import subprocess
    import sys

    code = (
        "import subprocess, sys\n"
        "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'],"
        " start_new_session=True, stdin=subprocess.DEVNULL,"
        " stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
        "print(p.pid, flush=True)\n"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         timeout=30, check=True)
    return int(out.stdout.strip())


def test_real_orphan_group_is_reaped_with_default_probes(tmp_path):
    import os

    from garuda.runtime.recovery import _process_identity, _process_live

    store = SessionStore(tmp_path)
    _begin(store)
    store.ensure_unified("s1")
    pid = _spawn_orphan_group()
    try:
        assert _process_live(pid) is True
        record_child(store, "s1", runtime_id="native", pid=pid)
        with pytest.raises(RecoveryError, match="belongs to live Garuda process"):
            recover(store, "s1")  # this test process is the recorded owner
        # Treat the recorded owner (this process) as crashed; everything else —
        # liveness, identity, group check, SIGKILL, death polling — is real.
        report = recover(
            store,
            "s1",
            identify=lambda probe: None if probe == os.getpid() else _process_identity(probe),
        )
        assert report.reaped_pids == (pid,)
        assert _process_live(pid) is False
        assert store.load_meta("s1")["runtime_children"][0]["state"] == "reaped"
    finally:
        try:
            os.killpg(pid, 9)
        except OSError:
            pass


def test_record_cancel_appends_under_the_meta_lock(tmp_path):
    import threading

    store = SessionStore(tmp_path)
    _begin(store)
    threads = [
        threading.Thread(target=record_cancel, args=(store, "s1"),
                         kwargs={"boundary": "turn", "reason": f"r{i}"})
        for i in range(16)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    reasons = {entry["reason"] for entry in store.load_meta("s1")["cancellations"]}
    assert reasons == {f"r{i}" for i in range(16)}


def test_update_active_runtime_segment_refuses_a_foreign_runtime(tmp_path):
    from garuda.runtime.session import RuntimeSegment

    store = SessionStore(tmp_path)
    _begin(store)
    store.ensure_unified("s1")
    with pytest.raises(ValueError, match="active runtime is 'native'"):
        store.update_active_runtime_segment(
            "s1", RuntimeSegment(runtime_id="codex", kind="acp", native_session_id="x")
        )
    assert store.load_unified("s1").active.runtime_id == "native"


def test_authority_snapshot_rejects_duplicate_families():
    from garuda.runtime.session import UnifiedSessionError, validate_authority_snapshot

    complete = {"edit=garuda", "terminal=garuda", "mcp=garuda", "approval=garuda"}
    validate_authority_snapshot(frozenset(complete))
    with pytest.raises(UnifiedSessionError, match="duplicate authority snapshot family"):
        validate_authority_snapshot(frozenset({*complete, "edit=agent"}))
    with pytest.raises(UnifiedSessionError, match="every family"):
        validate_authority_snapshot(frozenset(complete - {"mcp=garuda"}))


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
        assert store.load_meta("s1")["cancellations"][-1]["boundary"] == boundary
    # Append-only audit evidence: every boundary survives, oldest first.
    assert [c["boundary"] for c in store.load_meta("s1")["cancellations"]] == [
        "turn",
        "switch",
        "process",
    ]
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
    record_child(store, "s1", runtime_id="native", pid=1234, identify=_fake_identity)
    # PermissionError (another owner's process) is unknown, not dead.
    monkeypatch.setattr(_os, "kill", lambda pid, sig: (_ for _ in ()).throw(PermissionError()))
    assert recovery_mod._process_live(1234) is None
    with pytest.raises(RecoveryError, match="indeterminate liveness"):
        recover(store, "s1", identify=_child_only(1234))
    # ...and an injected unknown verdict refuses the same way.
    monkeypatch.setattr(_os, "kill", lambda pid, sig: None)
    assert recovery_mod._process_live(1234) is True
    with pytest.raises(RecoveryError, match="indeterminate liveness"):
        recover(store, "s1", is_alive=lambda pid: None, identify=_child_only(1234))


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
    (entry,) = store.load_meta("switch-cancel")["cancellations"]
    assert (entry["boundary"], entry["reason"]) == ("switch", "operator stopped handoff")
    # A refused cancel of a terminal transaction leaves the audit untouched.
    from garuda.runtime.handoff import HandoffError

    with pytest.raises(HandoffError, match="terminal"):
        await tx.cancel(source, reason="second cancel")
    assert len(store.load_meta("switch-cancel")["cancellations"]) == 1


async def test_handoff_cancel_audit_failure_still_cleans_up(tmp_path):
    from garuda.runtime.fake import FakeRuntime, FakeScenario
    from garuda.runtime.handoff import HandoffError, HandoffPhase, HandoffTransaction
    from garuda.runtime.protocol import LifecycleState

    store = SessionStore(tmp_path)
    _begin(store, "switch-audit")
    source = FakeRuntime(FakeScenario.SUCCESS, runtime_id="native")
    await source.start(task="t", session_id="switch-audit")
    target = FakeRuntime(FakeScenario.SUCCESS, runtime_id="target")
    tx = HandoffTransaction(session_id="switch-audit", store=store)
    await tx.begin(source, checkpoint=lambda: None, capture=lambda: {}, generate=lambda: None)
    await tx.start_target(source, target)

    def _broken(*_a, **_k):
        raise OSError("disk full")

    store.mutate_meta = _broken
    with pytest.raises(HandoffError, match="audit record failed"):
        await tx.cancel(source, target, reason="operator stop")
    assert target.state is LifecycleState.CLOSED
    assert source.state is LifecycleState.IDLE
    assert tx.phase is HandoffPhase.FAILED


def test_reap_refuses_a_pid_that_no_longer_leads_its_group(monkeypatch):
    """A live recorded PID whose group changed is not provably Garuda's child."""
    import garuda.runtime.recovery as recovery

    signalled: list[int] = []
    monkeypatch.setattr(recovery.os, "getpgid", lambda pid: pid + 1)
    monkeypatch.setattr(recovery.os, "killpg", lambda pid, sig: signalled.append(pid))
    with pytest.raises(recovery.RecoveryError, match="no longer leads"):
        recovery._reap_group(424242)
    assert signalled == []


def test_exited_children_are_never_probed(tmp_path):
    from garuda.runtime.recovery import record_child, record_child_exit, recover

    store = SessionStore(tmp_path)
    store.begin("s-exit", task="t", model="m", agent="a", workspace="w")
    store.checkpoint_messages("s-exit", [])
    store.ensure_unified("s-exit")
    record_child(store, "s-exit", runtime_id="native", pid=424242, identify=_fake_identity)
    record_child_exit(store, "s-exit", pid=424242)
    probed: list[int] = []
    report = recover(store, "s-exit", is_alive=lambda pid: probed.append(pid) or True)
    assert probed == []
    assert report.reaped_pids == ()


def _native_runtime(store):
    from garuda.core.loop import DefaultAgent
    from garuda.core.permissions import PermissionEngine
    from garuda.model.script_model import ScriptModel
    from garuda.runtime.native import NativeGarudaRuntime
    from garuda.tools import tools_for_names
    from garuda.types import AgentConfig

    return NativeGarudaRuntime(
        agent=DefaultAgent(),
        model=ScriptModel(responses=[]),
        tools=tools_for_names(["task_complete"]),
        config=AgentConfig(),
        permissions=PermissionEngine(mode="yolo"),
        store=store,
    )


async def test_native_cancel_records_the_turn_boundary(tmp_path):
    store = SessionStore(tmp_path)
    runtime = _native_runtime(store)
    await runtime.start(task="t", session_id="native-cancel")
    await runtime.cancel(reason="operator stop")
    (entry,) = store.load_meta("native-cancel")["cancellations"]
    assert (entry["boundary"], entry["reason"]) == ("turn", "operator stop")


async def test_native_cancel_audit_failure_still_cancels(tmp_path):
    import asyncio

    from garuda.runtime.protocol import LifecycleState, RuntimeCancelledError
    from garuda.runtime.recovery import CancellationAuditError

    store = SessionStore(tmp_path)
    idle = _native_runtime(store)
    await idle.start(task="t", session_id="native-idle")
    running = _native_runtime(store)
    await running.start(task="t", session_id="native-running")
    release = asyncio.Event()
    entered = asyncio.Event()

    async def _driver(*, task, turn, trail):
        entered.set()
        await release.wait()
        return None

    running.install_driver(_driver)
    prompting = asyncio.ensure_future(running.prompt("go"))
    await entered.wait()

    def _broken(*_a, **_k):
        raise OSError("disk full")

    store.mutate_meta = _broken
    with pytest.raises(CancellationAuditError, match="disk full"):
        await idle.cancel(reason="stop")
    assert idle.state is LifecycleState.CLOSED
    with pytest.raises(CancellationAuditError):
        await running.cancel(reason="stop")
    release.set()
    # The cancel still landed at the turn boundary despite the audit failure.
    with pytest.raises(RuntimeCancelledError, match="turn boundary"):
        await prompting
    assert running.state is LifecycleState.CLOSED


def _blocking_model():
    import asyncio

    from garuda.model.script_model import ScriptModel

    class _Blocking(ScriptModel):
        started = asyncio.Event()

        async def complete(self, *args, **kwargs):
            self.started.set()
            await asyncio.Event().wait()

        async def stream(self, *args, **kwargs):
            self.started.set()
            await asyncio.Event().wait()
            yield  # pragma: no cover

    return _Blocking(responses=[])


def _runner_kwargs(tmp_path, session_id, model):
    from garuda.core.events import EventStore
    from garuda.core.loop import DefaultAgent
    from garuda.core.permissions import PermissionEngine
    from garuda.tools import tools_for_names
    from garuda.types import AgentConfig

    (tmp_path / "ws").mkdir(exist_ok=True)
    return dict(
        model=model,
        agent=DefaultAgent(),
        tools=tools_for_names(["task_complete"]),
        config=AgentConfig(max_turns=5, enable_verifier=False, permission_mode="yolo"),
        permissions=PermissionEngine(mode="yolo"),
        workspace=str(tmp_path / "ws"),
        events=EventStore(session_id=session_id),
        store=SessionStore(tmp_path / "sessions"),
    )


async def test_runner_task_cancellation_records_the_turn_boundary(tmp_path):
    import asyncio

    from garuda.interfaces.runner import run_agent_task

    model = _blocking_model()
    task = asyncio.ensure_future(
        run_agent_task(task="block", **_runner_kwargs(tmp_path, "runner-cancel", model))
    )
    await asyncio.wait_for(model.started.wait(), 15)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    entries = SessionStore(tmp_path / "sessions").load_meta("runner-cancel")["cancellations"]
    # The facade's own record comes first; runtime teardown may append more.
    assert (entries[0]["boundary"], entries[0]["reason"]) == ("turn", "task cancelled")


async def test_resume_refuses_while_another_garuda_owns_the_session(tmp_path):
    """`--resume` against a session a live Garuda still runs must not reap that
    instance's child; refusal releases this run's lease."""
    import subprocess
    import sys

    from garuda.interfaces.runner import run_agent_task
    from garuda.model.protocol import ModelResponse
    from garuda.model.script_model import ScriptModel
    from garuda.runtime.recovery import _process_live
    from garuda.types import ToolCall
    from garuda.workspace.lease import LeaseStore

    def _script():
        return ScriptModel(responses=[ModelResponse(
            content=None,
            tool_calls=[ToolCall(id="1", name="task_complete", arguments={"summary": "ok"})],
        )])

    first = await run_agent_task(task="first", **_runner_kwargs(tmp_path, "owned", _script()))
    assert first.success
    store = SessionStore(tmp_path / "sessions")
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)"], start_new_session=True
    )
    try:
        # This test process plays the live owner that launched the child.
        record_child(store, "owned", runtime_id="native", pid=child.pid)
        with pytest.raises(RecoveryError, match="belongs to live Garuda process"):
            await run_agent_task(
                task="second", resume="owned",
                **_runner_kwargs(tmp_path, "resumer", _script()),
            )
        assert _process_live(child.pid) is True
        assert store.load_meta("owned")["runtime_children"][0]["state"] == "live"
        # The refused run released its own lease.
        assert LeaseStore().holders_of(tmp_path / "ws") == []
    finally:
        child.kill()
        child.wait()

    # A live lease naming the session (another instance, another workspace).
    store.update_meta("owned", {"runtime_children": []})
    leases = LeaseStore()
    leases.acquire(tmp_path / "other-ws", "owned", mode="mutating")
    with pytest.raises(RecoveryError, match="live mutating workspace lease"):
        await run_agent_task(
            task="third", resume="owned", **_runner_kwargs(tmp_path, "resumer-2", _script())
        )
    leases.release(tmp_path / "other-ws", "owned")
