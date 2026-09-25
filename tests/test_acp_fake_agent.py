"""Fake-agent conformance tests for issue #24 (P0.15).

The generic adapter runs the shared lifecycle suite against the fake server;
capability profiles negotiate to the expected authority; malformed, slow,
exiting, approval, resume, and version-mismatch behaviors are pinned. No
network, no subscription — every process here is spawned locally.
"""

import asyncio
import sys

import pytest

from garuda.acp.adapter import AcpRuntime
from garuda.acp.authority import AuthorityOwner
from garuda.acp.protocol import AcpCancelledError, AcpError, AcpProtocolError, AcpTimeoutError
from garuda.core.sessions import SessionStore
from garuda.runtime import LifecycleState, RuntimeClosedError
from garuda.runtime.protocol import RuntimeStartError
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
    with pytest.raises(AcpProtocolError, match="99.99"):
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
    await approval.permission_response(approval_id="a1", allow=True)
    await asyncio.wait_for(prompting, 15)
    assert approval.state is LifecycleState.IDLE
    await approval.close()

    cancelling = _adapter("slow")
    await cancelling.start(task="t", session_id="a2")
    prompting = asyncio.ensure_future(cancelling.prompt("slow work"))
    await asyncio.sleep(0.5)
    await cancelling.cancel(reason="user stopped")
    with pytest.raises(AcpCancelledError):
        await asyncio.wait_for(prompting, 15)
    assert cancelling.state is LifecycleState.CLOSED
    with pytest.raises(RuntimeClosedError):
        await cancelling.prompt("too late")
    with pytest.raises(RuntimeStartError):
        await cancelling.resume(native_session_id="elsewhere")


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

    prompting = asyncio.ensure_future(runtime.prompt("slow work"))
    await asyncio.sleep(0.2)
    await runtime.cancel(reason="operator stop")
    with pytest.raises(AcpCancelledError):
        await asyncio.wait_for(prompting, 15)
    assert store.load_meta("persisted-acp")["cancellation"] == {
        "boundary": "process",
        "reason": "operator stop",
    }
    await runtime.close()
    from garuda.runtime.recovery import recover

    # The stored authority snapshot and child identity are sufficient for the
    # production recovery path; a dead child is observed, not re-signalled.
    assert (await asyncio.to_thread(recover, store, "persisted-acp")).state.value == "resumable"
