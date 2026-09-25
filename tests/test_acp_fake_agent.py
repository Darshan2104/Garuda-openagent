"""Fake-agent conformance tests for issue #24 (P0.15).

The generic adapter runs the shared lifecycle suite against the fake server;
capability profiles negotiate to the expected authority; malformed, slow,
exiting, approval, resume, and version-mismatch behaviors are pinned. No
network, no subscription — every process here is spawned locally.
"""

import asyncio
import os
import sys

import pytest

from garuda.acp.adapter import AcpRuntime
from garuda.acp.authority import AuthorityOwner
from garuda.acp.protocol import AcpCancelledError, AcpError, AcpProtocolError, AcpTimeoutError
from garuda.core.sessions import SessionStore
from garuda.runtime import HealthStatus, LifecycleState, RuntimeClosedError
from garuda.runtime.protocol import RuntimeProtocolError, RuntimeStartError
from garuda.runtime.session import RuntimeSegment
from tests.test_runtime_conformance import run_conformance_suite

FAKE = [sys.executable, "-m", "garuda.acp.fake_agent"]


def test_public_profile_sets_are_pinned():
    """Set equality over the fake's contract cases.

    A renamed or silently removed profile must fail here by name — not slip
    through because no test happened to exercise that scenario.
    """
    from garuda.acp.fake_agent import BASE_PROFILES, CAPABILITY_PROFILES, PROFILES

    assert set(CAPABILITY_PROFILES) == {"full", "sandbox-only", "read-only"}
    assert set(BASE_PROFILES) == {
        "success",
        "streaming",
        "approval",
        "diff",
        "malformed",
        "slow",
        "exit-early",
        "resume",
        "version-mismatch",
        "odd-stop",
        "cancel-stop",
    }
    assert set(PROFILES) == set(BASE_PROFILES) | {
        "capabilities-full",
        "capabilities-sandbox-only",
        "capabilities-read-only",
    }


def _adapter(profile: str, **kwargs) -> AcpRuntime:
    return AcpRuntime([*FAKE, "--profile", profile], runtime_id=f"fake-{profile}", **kwargs)


async def test_adapter_passes_the_shared_conformance_suite():
    await run_conformance_suite(lambda: _adapter("success"))


async def test_capability_profiles_negotiate_expected_authority():
    full = _adapter("capabilities-full")
    await full.start(task="t", session_id="c1")
    assert full.authority is not None
    assert full.authority.owner_of("terminal") is AuthorityOwner.AGENT
    await full.close()

    sandbox_only = _adapter("capabilities-sandbox-only")
    await sandbox_only.start(task="t", session_id="c2")
    assert sandbox_only.authority is not None
    assert sandbox_only.authority.owner_of("edit") is AuthorityOwner.AGENT
    await sandbox_only.close()

    read_only = _adapter("capabilities-read-only")
    await read_only.start(task="t", session_id="c3")
    assert read_only.authority is not None
    assert read_only.authority.owner_of("terminal") is AuthorityOwner.GARUDA
    await read_only.close()


async def test_streaming_diff_and_resume_profiles(tmp_path):
    streaming = _adapter("streaming")
    await streaming.start(task="t", session_id="st1")
    await streaming.prompt("stream it")
    events, _ = await streaming.poll_events(0)
    chunks = [e for e in events if e.payload.get("chunk")]
    assert "".join(c.payload["chunk"] for c in chunks) == "hello, world"
    await streaming.close()

    diff = _adapter("diff")
    await diff.start(task="t", session_id="d1")
    await diff.prompt("change a.py")
    events, _ = await diff.poll_events(0)
    assert any(e.payload.get("path") == "a.py" for e in events)
    await diff.close()

    state_file = str(tmp_path / "fake-state.json")
    first = AcpRuntime(
        [*FAKE, "--profile", "resume", "--state-file", state_file],
        runtime_id="fake-resume",
    )
    await first.start(task="t", session_id="r1")
    first_id = first.native_session_id
    await first.close()
    second = AcpRuntime(
        [*FAKE, "--profile", "resume", "--state-file", state_file],
        runtime_id="fake-resume",
    )
    await second.start(task="t", session_id="r2")
    assert second.native_session_id == first_id
    await second.close()


async def test_malformed_slow_exit_and_version_pinned():
    malformed = _adapter("malformed")
    await malformed.start(task="t", session_id="m1")
    with pytest.raises(AcpProtocolError):
        await malformed.prompt("x")
    assert await malformed.health() is HealthStatus.UNAVAILABLE
    await malformed.close()

    slow = _adapter("slow")
    await slow.start(task="t", session_id="s1")
    with pytest.raises(AcpTimeoutError):
        await slow.prompt("x", timeout=3)
    assert slow.state is LifecycleState.FAILED
    await slow.close()

    exiting = _adapter("exit-early")
    await exiting.start(task="t", session_id="e1")
    with pytest.raises(AcpError):
        await asyncio.wait_for(exiting.prompt("x"), 15)
    await exiting.close()

    mismatch = _adapter("version-mismatch")
    with pytest.raises(AcpProtocolError, match="got 99"):
        await mismatch.start(task="t", session_id="v1")
    await mismatch.close()


async def test_approval_flow_and_cancel_paths():
    approval = _adapter("approval")
    await approval.start(task="t", session_id="a1")
    prompting = asyncio.ensure_future(approval.prompt("delete it"))
    for _ in range(100):
        await asyncio.sleep(0.05)
        events, _ = await approval.poll_events(0)
        requests = [e for e in events if e.kind.value == "approval_request"]
        if requests:
            break
    assert requests, "fake agent must request approval"
    request = requests[0].payload
    assert request["tool_call_id"] == "c-apr"
    assert {o["kind"] for o in request["options"]} == {"allow_once", "allow_always", "reject_once"}
    await approval.permission_response(approval_id=request["approval_id"], allow=True)
    await asyncio.wait_for(prompting, 15)
    assert approval.state is LifecycleState.IDLE
    events, _ = await approval.poll_events(0)
    [result] = [e for e in events if e.kind.value == "tool_result"]
    # `allow_once` was selected — never the wider `allow_always`.
    assert (result.payload["status"], result.payload["content"]) == ("completed", "approved")
    with pytest.raises(RuntimeProtocolError):
        await approval.permission_response(approval_id=request["approval_id"], allow=True)
    await approval.close()

    cancelling = _adapter("slow")
    await cancelling.start(task="t", session_id="a2")
    prompting = asyncio.ensure_future(cancelling.prompt("slow work"))
    await asyncio.sleep(0.5)
    await cancelling.cancel(reason="user stopped")
    with pytest.raises(AcpCancelledError):
        await asyncio.wait_for(prompting, 15)
    assert cancelling.state is LifecycleState.CLOSED
    assert await cancelling.health() is HealthStatus.UNAVAILABLE
    with pytest.raises(RuntimeClosedError):
        await cancelling.prompt("too late")
    with pytest.raises(RuntimeStartError):
        await cancelling.resume(native_session_id="elsewhere")


async def test_approval_handler_answers_v1_permission_requests():
    """An injected handler answers `session/request_permission` exactly once;
    a denial selects a reject option and the agent reports the call failed."""
    asked: list[str] = []

    async def deny(action: str) -> bool:
        asked.append(action)
        return False

    runtime = _adapter("approval", approval_handler=deny)
    await runtime.start(task="t", session_id="h1")
    await asyncio.wait_for(runtime.prompt("drop the table"), 15)
    assert asked == ["drop the table"]
    events, _ = await runtime.poll_events(0)
    [result] = [e for e in events if e.kind.value == "tool_result"]
    assert (result.payload["status"], result.payload["content"]) == ("failed", "denied")
    await runtime.close()


async def test_cancel_answers_open_permission_requests_cancelled():
    runtime = _adapter("approval")
    await runtime.start(task="t", session_id="c1")
    prompting = asyncio.ensure_future(runtime.prompt("wait for me"))
    for _ in range(100):
        await asyncio.sleep(0.05)
        events, _ = await runtime.poll_events(0)
        if any(e.kind.value == "approval_request" for e in events):
            break
    await runtime.cancel(reason="user stopped")
    with pytest.raises(AcpCancelledError):
        await asyncio.wait_for(prompting, 15)
    assert runtime.state is LifecycleState.CLOSED


async def test_unknown_stop_reason_fails_and_reaps():
    """A stop the normalizer cannot close truthfully fails the runtime and
    reaps the process instead of leaving it RUNNING."""
    runtime = _adapter("odd-stop")
    await runtime.start(task="t", session_id="o1")
    with pytest.raises(AcpProtocolError, match="mystery"):
        await asyncio.wait_for(runtime.prompt("go"), 15)
    assert runtime.state is LifecycleState.FAILED
    assert await runtime.health() is HealthStatus.UNAVAILABLE


async def test_agent_returned_cancelled_closes_the_runtime():
    """A v1 agent may end a turn `cancelled` on its own; the session is over,
    so the runtime closes and reaps instead of idling on a dead trail."""
    runtime = _adapter("cancel-stop")
    await runtime.start(task="t", session_id="cs1")
    with pytest.raises(AcpCancelledError):
        await asyncio.wait_for(runtime.prompt("go"), 15)
    assert runtime.state is LifecycleState.CLOSED
    assert await runtime.health() is HealthStatus.UNAVAILABLE
    with pytest.raises(RuntimeClosedError):
        await runtime.prompt("again")


async def test_acp_process_identity_and_cancel_are_persisted(tmp_path):
    """A real spawned ACP child is recoverable only through its stored identity."""
    store = SessionStore(tmp_path / "sessions")
    store.begin("persisted-acp", task="t", model="m", agent="a", workspace="w")
    store.checkpoint_messages("persisted-acp", [])
    store.ensure_unified("persisted-acp")
    store.attach_runtime_segment(
        "persisted-acp",
        RuntimeSegment(runtime_id="fake-slow", kind="acp", native_session_id="pending"),
    )
    runtime = _adapter("slow", store=store)
    await runtime.start(task="t", session_id="persisted-acp")
    child = store.load_meta("persisted-acp")["runtime_children"][0]
    assert child["runtime_id"] == "fake-slow"
    assert child["pid"] > 1
    assert child["process_group"] == child["pid"]
    assert child["identity"] and child["owner"]["pid"] == os.getpid()
    from garuda.runtime.recovery import RecoveryError, recover

    # This process launched the child and is alive: recovery must refuse
    # rather than reap a live instance's runtime.
    with pytest.raises(RecoveryError, match="belongs to live Garuda process"):
        await asyncio.to_thread(recover, store, "persisted-acp")
    assert await runtime.health() is HealthStatus.OK

    prompting = asyncio.ensure_future(runtime.prompt("slow work"))
    await asyncio.sleep(0.2)
    await runtime.cancel(reason="operator stop")
    with pytest.raises(AcpCancelledError):
        await asyncio.wait_for(prompting, 15)
    (entry,) = store.load_meta("persisted-acp")["cancellations"]
    assert (entry["boundary"], entry["reason"]) == ("process", "operator stop")
    await runtime.close()
    # A clean close retires the record, so a restart never probes a PID the
    # OS may since have handed to an unrelated process.
    assert store.load_meta("persisted-acp")["runtime_children"][0]["state"] == "exited"
    # The stored authority snapshot and child identity are sufficient for the
    # production recovery path; a dead child is observed, not re-signalled.
    assert (await asyncio.to_thread(recover, store, "persisted-acp")).state.value == "resumable"


def _acp_session(store: SessionStore, session_id: str, runtime_id: str) -> None:
    store.begin(session_id, task="t", model="m", agent="a", workspace="w")
    store.checkpoint_messages(session_id, [])
    store.ensure_unified(session_id)
    store.attach_runtime_segment(
        session_id,
        RuntimeSegment(runtime_id=runtime_id, kind="acp", native_session_id="pending"),
    )


async def test_acp_cancel_and_close_survive_an_audit_failure(tmp_path):
    """A store outage never blocks cancellation or leaves the child running."""
    from garuda.runtime.recovery import CancellationAuditError

    store = SessionStore(tmp_path / "sessions")
    _acp_session(store, "audit-fail", "fake-slow")
    runtime = _adapter("slow", store=store)
    await runtime.start(task="t", session_id="audit-fail")
    prompting = asyncio.ensure_future(runtime.prompt("slow work"))
    await asyncio.sleep(0.2)

    def _broken(*_a, **_k):
        raise OSError("disk full")

    store.mutate_meta = _broken
    with pytest.raises(CancellationAuditError, match="disk full"):
        await runtime.cancel(reason="operator stop")
    # session/cancel still reached the agent: the prompt ends cancelled.
    with pytest.raises(AcpCancelledError):
        await asyncio.wait_for(prompting, 15)
    await runtime.close()

    second = _adapter("slow", store=store)
    _acp_session(store, "audit-fail-close", "fake-slow")
    del store.mutate_meta
    await second.start(task="t", session_id="audit-fail-close")
    prompting = asyncio.ensure_future(second.prompt("slow work"))
    await asyncio.sleep(0.2)
    store.mutate_meta = _broken
    with pytest.raises(CancellationAuditError):
        await second.close()
    assert await second.health() is HealthStatus.UNAVAILABLE
    with pytest.raises(AcpError):
        await asyncio.wait_for(prompting, 15)


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

async def main():
    store = SessionStore(sys.argv[1])
    runtime = AcpRuntime([sys.executable, '-c', sys.argv[3]], runtime_id='crash-acp', store=store)
    await runtime.start(task='t', session_id=sys.argv[2])
    print('ready', flush=True)
    await asyncio.sleep(120)

asyncio.run(main())
"""


async def test_crashed_owner_leaves_an_orphan_that_recovery_reaps(tmp_path):
    """A real crash: the Garuda-side owner is SIGKILLed with its ACP child
    running; restart recovery with the default probes reaps that child by
    its persisted identity."""
    import subprocess
    from pathlib import Path

    from garuda.runtime.recovery import _process_live, recover

    store = SessionStore(tmp_path / "sessions")
    _acp_session(store, "crash", "crash-acp")
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
        (record,) = store.load_meta("crash")["runtime_children"]
        assert record["owner"]["pid"] == owner.pid
        pid = record["pid"]
        owner.kill()  # the crash: no close(), no record retirement
        owner.wait()
        assert _process_live(pid) is True
        report = await asyncio.to_thread(recover, store, "crash")
        assert report.reaped_pids == (pid,)
        assert _process_live(pid) is False
        assert store.load_meta("crash")["runtime_children"][0]["state"] == "reaped"
    finally:
        if owner.poll() is None:
            owner.kill()
            owner.wait()
        if pid is not None:
            try:
                os.killpg(pid, 9)
            except OSError:
                pass
