"""A handoff target's child is recorded from launch, before ownership moves.

A CLI or SDK handoff starts its target while the source still owns the
session, so the target cannot be recorded `live` until the acknowledgement.
It is recorded `prepared` at launch instead, bound to the handoff attempt,
promoted in place when bound, and reaped by `recover()` under the same owner,
lease, and identity gates as a live child when Garuda crashes in between.
"""

import asyncio
import os
import subprocess
import sys
from pathlib import Path

import pytest

from garuda.acp.adapter import AcpRuntime
from garuda.core.sessions import SessionStore
from garuda.runtime.recovery import (
    PreparedChildRecorder,
    RecoveryError,
    RestartState,
    reclaim_native,
    record_child,
    record_prepared_child,
    recover,
    refresh_prepared_child,
)
from garuda.runtime.session import RuntimeSegment

FAKE = [sys.executable, "-m", "garuda.acp.fake_agent"]


def _identity(pid):
    return f"identity-{pid}"


def _child_only(pid, identity=None):
    """Identify only the child; its recorded owner reads as gone."""
    return lambda probe: (identity or _identity(pid)) if probe == pid else None


def _session(store, sid="s1", *, target="codex", handoff="prepared"):
    store.begin(sid, task="t", model="m", agent="a", workspace="w")
    store.checkpoint_messages(sid, [])
    store.ensure_unified(sid)
    store.record_handoff(sid, state=handoff, attempts=1, target_runtime=target)


def _acp_segment(runtime_id="codex"):
    from garuda.acp.authority import AuthorityMap

    owners = {family: "agent" for family in ("edit", "terminal", "mcp", "approval")}
    return RuntimeSegment(
        runtime_id=runtime_id, kind="acp", native_session_id="agent-1",
        capabilities=AuthorityMap(owners=owners).to_snapshot(),
    )


def _prepared(store, sid="s1", *, pid=4242, runtime_id="codex"):
    record_prepared_child(store, sid, runtime_id=runtime_id, pid=pid, identify=_identity)


def _children(store, sid="s1"):
    return store.load_meta(sid)["runtime_children"]


# --- recording --------------------------------------------------------------


def test_prepared_child_binds_only_to_the_prepared_target(tmp_path):
    store = SessionStore(tmp_path)
    _session(store)
    _prepared(store)
    (record,) = _children(store)
    assert record["state"] == "prepared"
    assert record["runtime_id"] == "codex"
    assert record["identity"] == "identity-4242"

    with pytest.raises(RecoveryError, match="not the prepared handoff target"):
        _prepared(store, pid=4343, runtime_id="claude")
    store.record_handoff("s1", state="failed", attempts=1, target_runtime="codex")
    with pytest.raises(RecoveryError, match="not the prepared handoff target"):
        _prepared(store, pid=4343)
    for bad in (0, 1, True, os.getpid()):
        _session(store, "s2")
        with pytest.raises(RecoveryError):
            _prepared(store, "s2", pid=bad)


def test_refresh_replaces_the_launch_identity(tmp_path):
    store = SessionStore(tmp_path)
    _session(store)
    _prepared(store)
    refresh_prepared_child(
        store, "s1", runtime_id="codex", pid=4242, identify=lambda _pid: "agent-identity"
    )
    assert _children(store)[0]["identity"] == "agent-identity"
    with pytest.raises(RecoveryError, match="no prepared child 4343"):
        refresh_prepared_child(store, "s1", runtime_id="codex", pid=4343, identify=_identity)


def test_binding_promotes_the_prepared_record_in_place(tmp_path):
    store = SessionStore(tmp_path)
    _session(store)
    _prepared(store)
    store.record_handoff(
        "s1", state="acknowledged", attempts=1, target_runtime="codex",
        active_segment=_acp_segment(),
    )
    record_child(store, "s1", runtime_id="codex", pid=4242, identify=_identity)
    (record,) = _children(store)
    assert record["state"] == "live"
    assert record["identity"] == "identity-4242"


def test_promotion_refuses_a_changed_identity(tmp_path):
    store = SessionStore(tmp_path)
    _session(store)
    _prepared(store)
    store.record_handoff(
        "s1", state="acknowledged", attempts=1, target_runtime="codex",
        active_segment=_acp_segment(),
    )
    with pytest.raises(RecoveryError, match="changed identity before binding"):
        record_child(store, "s1", runtime_id="codex", pid=4242, identify=lambda _p: "other")
    assert _children(store)[0]["state"] == "prepared"


# --- recovery ---------------------------------------------------------------


def test_crash_before_acknowledgement_reaps_the_prepared_child(tmp_path):
    store = SessionStore(tmp_path)
    _session(store)
    _prepared(store)
    live = {4242}
    report = recover(
        store, "s1",
        is_alive=lambda pid: pid in live,
        reap=live.discard,
        identify=_child_only(4242),
    )
    assert report.state is RestartState.ROLLED_BACK
    assert report.reaped_pids == (4242,)
    assert _children(store)[0]["state"] == "reaped"
    handoff = store.load_unified("s1").handoff
    assert handoff["state"] == "failed"
    assert handoff["target_runtime"] == "codex"


def test_prepared_child_with_a_live_owner_refuses_recovery(tmp_path):
    store = SessionStore(tmp_path)
    _session(store)
    _prepared(store)
    signalled = []
    with pytest.raises(RecoveryError):
        recover(store, "s1", is_alive=lambda _pid: True, reap=signalled.append,
                identify=_identity)
    assert signalled == []
    assert store.load_unified("s1").handoff["state"] == "prepared"


def test_launcher_identity_mismatch_is_never_signalled(tmp_path):
    """A crash before the post-handshake refresh fails safe: no signal."""
    store = SessionStore(tmp_path)
    _session(store)
    _prepared(store)
    signalled = []
    report = recover(store, "s1", is_alive=lambda _pid: True, reap=signalled.append,
                     identify=_child_only(4242, "the-real-agent"))
    assert signalled == []
    assert _children(store)[0]["state"] == "reused"
    assert report.state is RestartState.ROLLED_BACK


def test_failed_startup_record_keeps_a_leftover_prepared_child_bound(tmp_path):
    store = SessionStore(tmp_path)
    _session(store)
    _prepared(store)
    store.record_handoff("s1", state="failed", attempts=1, reason="target_startup",
                         target_runtime="codex")
    live = {4242}
    report = recover(store, "s1", is_alive=lambda pid: pid in live, reap=live.discard,
                     identify=_child_only(4242))
    assert report.state is RestartState.RESUMABLE
    assert report.reaped_pids == (4242,)


def test_a_later_handoff_attempt_never_unbinds_a_leftover_prepared_child(tmp_path):
    """The binding is append-only: a retry naming another target (or no
    target at all) must not strand a record that is still `prepared`."""
    store = SessionStore(tmp_path)
    _session(store)
    _prepared(store)
    store.record_handoff("s1", state="failed", attempts=1)
    store.record_handoff("s1", state="prepared", attempts=1, target_runtime="claude")
    assert store.load_meta("s1")["prepared_targets"] == ["codex"]
    live = {4242}
    report = recover(store, "s1", is_alive=lambda pid: pid in live, reap=live.discard,
                     identify=_child_only(4242))
    assert report.reaped_pids == (4242,)
    assert report.state is RestartState.ROLLED_BACK


def test_unbound_prepared_child_fails_closed(tmp_path):
    store = SessionStore(tmp_path)
    _session(store)
    _prepared(store)
    store.update_meta("s1", {"prepared_targets": []})
    with pytest.raises(RecoveryError, match="not bound to a session runtime"):
        recover(store, "s1", is_alive=lambda _pid: False, reap=lambda _pid: None,
                identify=_child_only(4242))


def test_crash_between_acknowledgement_and_binding_is_reclaimable(tmp_path):
    """The ack moved ownership but the child was never promoted: recovery
    reaps it by its prepared record, and reclaim can then prove it stopped."""
    store = SessionStore(tmp_path)
    _session(store)
    _prepared(store)
    store.record_handoff(
        "s1", state="acknowledged", attempts=1, target_runtime="codex",
        active_segment=_acp_segment(),
    )
    live = {4242}
    probes = {"is_alive": lambda pid: pid in live, "reap": live.discard,
              "identify": _child_only(4242)}
    report = recover(store, "s1", **probes)
    assert report.state is RestartState.EXTERNAL
    assert report.reaped_pids == (4242,)
    reclaimed = reclaim_native(store, "s1", **probes)
    assert reclaimed.state is RestartState.RESUMABLE
    assert store.load_unified("s1").active.runtime_id == "native"


# --- adapter and handoff wiring ---------------------------------------------


async def test_adapter_records_retires_and_refuses_misuse(tmp_path):
    store = SessionStore(tmp_path)
    _session(store, target="fake-mismatch")
    runtime = AcpRuntime([*FAKE, "--profile", "version-mismatch"], runtime_id="fake-mismatch")
    runtime.record_launch_with(PreparedChildRecorder(store, "s1", "fake-mismatch"))
    with pytest.raises(Exception, match="version"):
        await runtime.start(task="t", session_id="s1")
    (record,) = _children(store)
    # Recorded at launch, reaped by the failed start, then retired.
    assert record["state"] == "exited"

    with pytest.raises(Exception, match="before start"):
        runtime.record_launch_with(PreparedChildRecorder(store, "s1", "fake-mismatch"))
    backed = AcpRuntime([*FAKE, "--profile", "success"], runtime_id="x", store=store)
    with pytest.raises(Exception, match="records its child itself"):
        backed.record_launch_with(PreparedChildRecorder(store, "s1", "x"))


async def test_cancelled_start_reaps_and_retires_the_child(tmp_path):
    from garuda.runtime.recovery import _process_live

    store = SessionStore(tmp_path)
    _session(store, target="silent")
    # Never answers the handshake, so `start` is parked in `initialize()`.
    runtime = AcpRuntime([sys.executable, "-c", "import time; time.sleep(60)"],
                         runtime_id="silent")
    runtime.record_launch_with(PreparedChildRecorder(store, "s1", "silent"))
    starting = asyncio.ensure_future(runtime.start(task="t", session_id="s1"))
    for _ in range(200):
        if store.load_meta("s1").get("runtime_children"):
            break
        await asyncio.sleep(0.02)
    (record,) = _children(store)
    assert record["state"] == "prepared"
    starting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await starting
    assert _process_live(record["pid"]) is False
    assert _children(store)[0]["state"] == "exited"


async def test_a_failed_retirement_leaves_a_recoverable_record(tmp_path):
    store = SessionStore(tmp_path)
    _session(store, target="fake-mismatch")

    class Flaky(PreparedChildRecorder):
        def exited(self, pid):
            raise OSError("disk full")

    runtime = AcpRuntime([*FAKE, "--profile", "version-mismatch"], runtime_id="fake-mismatch")
    runtime.record_launch_with(Flaky(store, "s1", "fake-mismatch"))
    with pytest.raises(Exception, match="version"):
        await runtime.start(task="t", session_id="s1")
    (record,) = _children(store)
    assert record["state"] == "prepared"
    # Later, with this owner gone and the child reaped, recovery retires the
    # record without a signal instead of refusing on it forever.
    report = recover(store, "s1", identify=lambda _pid: None)
    assert report.reaped_pids == ()
    assert _children(store)[0]["state"] == "exited"


async def test_acknowledgement_audit_failure_retires_a_real_target(tmp_path):
    from garuda.runtime.handoff import HandoffError, execute_handoff
    from tests.test_handoff import _native_source

    store = SessionStore(tmp_path / "sessions")
    source = await _native_source(tmp_path, store, "h3")
    original = store.record_handoff

    def failing(session_id, *, state, **kwargs):
        if state == "acknowledged":
            raise OSError("disk full")
        return original(session_id, state=state, **kwargs)

    store.record_handoff = failing
    with pytest.raises(HandoffError, match="acknowledge audit failed"):
        await execute_handoff(
            session_id="h3", source=source, store=store,
            target_factory=lambda: AcpRuntime(
                [*FAKE, "--profile", "success"], runtime_id="fake-success"
            ),
        )
    assert [child["state"] for child in _children(store, "h3")] == ["exited"]


async def test_unrecordable_target_never_runs(tmp_path):
    store = SessionStore(tmp_path)
    _session(store, target="someone-else")
    runtime = AcpRuntime([*FAKE, "--profile", "success"], runtime_id="fake-success")
    runtime.record_launch_with(PreparedChildRecorder(store, "s1", "fake-success"))
    with pytest.raises(RecoveryError, match="not the prepared handoff target"):
        await runtime.start(task="t", session_id="s1")
    assert "runtime_children" not in store.load_meta("s1")


async def test_execute_handoff_records_the_target_from_launch(tmp_path):
    from garuda.runtime.handoff import execute_handoff
    from tests.test_handoff import _native_source

    store = SessionStore(tmp_path / "sessions")
    source = await _native_source(tmp_path, store, "h1")
    seen = {}

    class Recording(AcpRuntime):
        async def start(self, **kwargs):
            seen["handoff"] = dict(store.load_unified("h1").handoff)
            info = await super().start(**kwargs)
            seen["child"] = dict(_children(store, "h1")[0])
            return info

    target = Recording([*FAKE, "--profile", "success"], runtime_id="fake-success")
    tx, bound = await execute_handoff(
        session_id="h1", source=source, target_factory=lambda: target, store=store,
    )
    assert seen["handoff"]["state"] == "prepared"
    assert seen["handoff"]["target_runtime"] == "fake-success"
    assert seen["child"]["state"] == "prepared"
    (record,) = _children(store, "h1")
    assert record["state"] == "live"
    assert record["pid"] == seen["child"]["pid"]
    await bound.close()
    assert _children(store, "h1")[0]["state"] == "exited"


async def test_execute_handoff_startup_failure_retires_and_keeps_the_target(tmp_path):
    from garuda.runtime.handoff import HandoffError, execute_handoff
    from tests.test_handoff import _native_source

    store = SessionStore(tmp_path / "sessions")
    source = await _native_source(tmp_path, store, "h2")
    with pytest.raises(HandoffError, match="target startup failed"):
        await execute_handoff(
            session_id="h2", source=source, store=store,
            target_factory=lambda: AcpRuntime(
                [*FAKE, "--profile", "version-mismatch"], runtime_id="fake-mismatch"
            ),
        )
    handoff = store.load_unified("h2").handoff
    assert handoff["state"] == "failed"
    assert handoff["target_runtime"] == "fake-mismatch"
    assert [child["state"] for child in _children(store, "h2")] == ["exited"]
    # The source never ran a turn; give it the checkpoint a real one would have.
    store.checkpoint_messages("h2", [])
    assert recover(store, "h2").state is RestartState.RESUMABLE


# --- a real crash -----------------------------------------------------------

_CRASH_CHILD = (
    "import time\n"
    "from garuda.acp.fake_agent import main\n"
    "main(['--profile', 'success'])\n"
    # Outlive stdin EOF, as a wedged agent would after its owner dies.
    "time.sleep(120)\n"
)

_CRASH_OWNER = """
import asyncio, sys
from garuda.acp.adapter import AcpRuntime
from garuda.core.sessions import SessionStore
from garuda.runtime.recovery import PreparedChildRecorder

async def main():
    store = SessionStore(sys.argv[1])
    runtime = AcpRuntime([sys.executable, '-c', sys.argv[3]], runtime_id='crash-acp')
    runtime.record_launch_with(PreparedChildRecorder(store, sys.argv[2], 'crash-acp'))
    await runtime.start(task='t', session_id=sys.argv[2])
    print('ready', flush=True)
    await asyncio.sleep(120)

asyncio.run(main())
"""


async def test_crash_before_acknowledgement_reaps_a_real_orphan(tmp_path):
    """The owner is SIGKILLed with a started but unbound handoff target; the
    default-probe recovery reaps that child by its prepared record."""
    from garuda.runtime.recovery import _process_live

    store = SessionStore(tmp_path / "sessions")
    _session(store, "crash", target="crash-acp")
    repo = Path(__file__).resolve().parents[1]
    owner = subprocess.Popen(
        [sys.executable, "-c", _CRASH_OWNER, str(tmp_path / "sessions"), "crash", _CRASH_CHILD],
        cwd=repo,
        stdout=subprocess.PIPE,
        text=True,
    )
    pid = None
    try:
        line = await asyncio.wait_for(asyncio.to_thread(owner.stdout.readline), 60)
        assert line.strip() == "ready"
        (record,) = _children(store, "crash")
        assert record["state"] == "prepared"
        assert record["owner"]["pid"] == owner.pid
        pid = record["pid"]
        owner.kill()
        owner.wait()
        assert _process_live(pid) is True
        report = await asyncio.to_thread(recover, store, "crash")
        assert report.state is RestartState.ROLLED_BACK
        assert report.reaped_pids == (pid,)
        assert _process_live(pid) is False
        assert _children(store, "crash")[0]["state"] == "reaped"
    finally:
        if owner.poll() is None:
            owner.kill()
            owner.wait()
        if pid is not None:
            try:
                os.killpg(pid, 9)
            except OSError:
                pass
