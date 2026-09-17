"""Protocol unit tests for issue #11 (P0.4).

Lifecycle transitions, typed failures, capability semantics, event correlation,
and the guarantee that no vendor/ACP types leak into the generic contract.
"""

import pathlib

import pytest

from garuda.runtime import (
    PROTOCOL_VERSION,
    AgentRuntime,
    AgentRuntimeError,
    AuthStatus,
    HealthStatus,
    LifecycleState,
    RuntimeCapabilities,
    RuntimeClosedError,
    RuntimeEvent,
    RuntimeEventKind,
    RuntimeInfo,
    RuntimeKind,
    RuntimeProtocolError,
    RuntimeStartError,
    RuntimeTimeoutError,
    RuntimeTransitionError,
    check_transition,
)
from garuda.runtime.fake import FakeRuntime, FakeScenario


def test_protocol_version_is_pinned():
    assert PROTOCOL_VERSION == "0.1"


def test_valid_transitions_pass():
    check_transition(LifecycleState.DISCOVERED, LifecycleState.STARTING)
    check_transition(LifecycleState.STARTING, LifecycleState.IDLE)
    check_transition(LifecycleState.IDLE, LifecycleState.RUNNING)
    check_transition(LifecycleState.IDLE, LifecycleState.PAUSED_AT_BOUNDARY)
    check_transition(LifecycleState.RUNNING, LifecycleState.IDLE)
    check_transition(LifecycleState.RUNNING, LifecycleState.PAUSED_AT_BOUNDARY)
    check_transition(LifecycleState.PAUSED_AT_BOUNDARY, LifecycleState.RUNNING)
    check_transition(LifecycleState.PAUSED_AT_BOUNDARY, LifecycleState.IDLE)
    check_transition(LifecycleState.IDLE, LifecycleState.CLOSED)


@pytest.mark.parametrize(
    ("frm", "to"),
    [
        (LifecycleState.DISCOVERED, LifecycleState.RUNNING),
        (LifecycleState.DISCOVERED, LifecycleState.CLOSED),
        (LifecycleState.IDLE, LifecycleState.STARTING),
        (LifecycleState.RUNNING, LifecycleState.STARTING),
        (LifecycleState.RUNNING, LifecycleState.CLOSED),
        (LifecycleState.CLOSED, LifecycleState.IDLE),
        (LifecycleState.FAILED, LifecycleState.IDLE),
        (LifecycleState.PAUSED_AT_BOUNDARY, LifecycleState.STARTING),
    ],
)
def test_invalid_transitions_raise_typed_error(frm, to):
    with pytest.raises(RuntimeTransitionError) as exc:
        check_transition(frm, to)
    assert exc.value.frm is frm
    assert exc.value.to is to


def test_failures_are_typed_agent_runtime_errors():
    for cls in (
        AgentRuntimeError,
        RuntimeStartError,
        RuntimeTimeoutError,
        RuntimeProtocolError,
        RuntimeClosedError,
    ):
        assert issubclass(cls, AgentRuntimeError)
    for cls in (RuntimeStartError, RuntimeTimeoutError, RuntimeProtocolError, RuntimeClosedError):
        assert cls is not AgentRuntimeError


def test_capabilities_keep_unknown_names():
    caps = RuntimeCapabilities(names=frozenset({"prompt", "future-cap"}))
    assert caps.supports("prompt")
    assert not caps.supports("cancel")
    assert "future-cap" in caps.names


def test_runtime_info_defaults():
    info = RuntimeInfo(runtime_id="r1", kind=RuntimeKind.NATIVE, version="0.1")
    assert info.health is HealthStatus.OK
    assert info.auth is AuthStatus.UNKNOWN
    assert info.native_session_id is None


def test_event_correlation_is_enforced():
    with pytest.raises(ValueError):
        RuntimeEvent(kind=RuntimeEventKind.MESSAGE, session_id="", turn=0, seq=0)
    with pytest.raises(ValueError):
        RuntimeEvent(kind=RuntimeEventKind.MESSAGE, session_id="s", turn=-1, seq=0)
    with pytest.raises(ValueError):
        RuntimeEvent(kind=RuntimeEventKind.MESSAGE, session_id="s", turn=0, seq=-1)


def test_terminal_lifecycle_detection():
    assert RuntimeEvent(
        kind=RuntimeEventKind.LIFECYCLE,
        session_id="s",
        turn=1,
        seq=0,
        payload={"state": "cancelled"},
    ).is_terminal()
    assert not RuntimeEvent(
        kind=RuntimeEventKind.LIFECYCLE,
        session_id="s",
        turn=1,
        seq=0,
        payload={"state": "completed"},
    ).is_terminal()
    assert not RuntimeEvent(
        kind=RuntimeEventKind.MESSAGE, session_id="s", turn=1, seq=0
    ).is_terminal()


def test_no_vendor_types_leak_into_the_generic_contract():
    package = pathlib.Path(__file__).resolve().parents[1] / "garuda" / "runtime"
    sources = sorted(package.glob("*.py"))
    assert sources, "garuda/runtime must contain modules"
    for path in sources:
        text = path.read_text()
        assert "import acp" not in text, path.name
        assert "from garuda.acp" not in text, path.name


async def test_fake_runtime_satisfies_the_protocol():
    assert isinstance(FakeRuntime(), AgentRuntime)


async def test_startup_failure_is_typed_and_terminal():
    rt = FakeRuntime(FakeScenario.STARTUP_FAILURE)
    with pytest.raises(RuntimeStartError):
        await rt.start(task="x")
    assert rt.state is LifecycleState.FAILED


async def test_start_rejects_empty_task_and_double_start():
    rt = FakeRuntime()
    with pytest.raises(RuntimeStartError):
        await rt.start(task="")
    await rt.start(task="x")
    with pytest.raises(RuntimeStartError):
        await rt.start(task="y")


async def test_resume_rejects_unknown_sessions():
    rt = FakeRuntime()
    with pytest.raises(RuntimeStartError):
        await rt.resume(native_session_id="nope")
    info = await rt.resume(native_session_id="fake-native-s1")
    assert info.native_session_id == "fake-native-s1"
