"""Deterministic in-process `AgentRuntime` for conformance tests (P0.4, #11).

No threads, no sleeps, no I/O: every outcome is scripted by `FakeScenario` and
advanced by ordinary method calls, so the same test passes identically on every
run. Every future adapter runs the shared suite in `test_runtime_conformance`.
"""

from __future__ import annotations

from enum import Enum

from garuda.runtime.events import RuntimeEvent, RuntimeEventKind
from garuda.runtime.protocol import (
    PROTOCOL_VERSION,
    AuthStatus,
    HealthStatus,
    LifecycleState,
    RuntimeCapabilities,
    RuntimeClosedError,
    RuntimeInfo,
    RuntimeKind,
    RuntimeNotActiveError,
    RuntimeProtocolError,
    RuntimeStartError,
    check_transition,
)


class FakeScenario(str, Enum):
    SUCCESS = "success"
    STREAMING = "streaming"
    APPROVAL = "approval"
    MALFORMED = "malformed"
    DELAYED_BOUNDARY = "delayed_boundary"
    MUTATION_ATTEMPT = "mutation_attempt"
    CANCELLATION = "cancellation"
    STARTUP_FAILURE = "startup_failure"


class FakeRuntime:
    """Scripted `AgentRuntime`. One instance, one session, fully deterministic."""

    def __init__(
        self,
        scenario: FakeScenario = FakeScenario.SUCCESS,
        runtime_id: str = "fake",
    ):
        self._scenario = scenario
        self._runtime_id = runtime_id
        self._state = LifecycleState.DISCOVERED
        self._native_session_id: str | None = None
        self._session_id = ""
        self._turn = 0
        self._events: list[RuntimeEvent] = []
        self._pause_pending = False
        self._approval_pending: str | None = None
        self._cancelled = scenario is FakeScenario.CANCELLATION

    @property
    def runtime_id(self) -> str:
        return self._runtime_id

    @property
    def kind(self) -> RuntimeKind:
        return RuntimeKind.NATIVE

    @property
    def version(self) -> str:
        return PROTOCOL_VERSION

    @property
    def state(self) -> LifecycleState:
        return self._state

    @property
    def native_session_id(self) -> str | None:
        return self._native_session_id

    @property
    def scenario(self) -> FakeScenario:
        return self._scenario

    async def health(self) -> HealthStatus:
        return HealthStatus.OK

    async def auth_status(self) -> AuthStatus:
        return AuthStatus.AUTHENTICATED

    def _move(self, to: LifecycleState) -> None:
        check_transition(self._state, to)
        self._state = to

    def _emit(self, kind: RuntimeEventKind, payload: dict | None = None) -> None:
        if self._state in (LifecycleState.CLOSED, LifecycleState.FAILED):
            raise RuntimeClosedError("runtime emitted past its terminal state")
        self._events.append(
            RuntimeEvent(
                kind=kind,
                session_id=self._session_id,
                turn=self._turn,
                seq=len(self._events),
                payload=dict(payload or {}),
            )
        )

    def _info(self) -> RuntimeInfo:
        return RuntimeInfo(
            runtime_id=self._runtime_id,
            kind=self.kind,
            version=self.version,
            capabilities=RuntimeCapabilities(names=frozenset({"prompt", "cancel"})),
            health=HealthStatus.OK,
            auth=AuthStatus.AUTHENTICATED,
            native_session_id=self._native_session_id,
        )

    async def start(self, *, task: str, session_id: str | None = None) -> RuntimeInfo:
        if self._scenario is FakeScenario.STARTUP_FAILURE:
            self._move(LifecycleState.STARTING)
            self._move(LifecycleState.FAILED)
            raise RuntimeStartError("fake startup failure")
        if self._state is not LifecycleState.DISCOVERED:
            raise RuntimeStartError(f"already started (state={self._state.value})")
        if not task:
            raise RuntimeStartError("task must not be empty")
        self._move(LifecycleState.STARTING)
        self._native_session_id = f"fake-native-{(session_id or 's1')}"
        self._session_id = session_id or "s1"
        self._move(LifecycleState.IDLE)
        self._emit(RuntimeEventKind.LIFECYCLE, {"state": "started", "task": task})
        return self._info()

    async def resume(self, *, native_session_id: str) -> RuntimeInfo:
        if self._state is not LifecycleState.DISCOVERED:
            raise RuntimeStartError(f"cannot resume (state={self._state.value})")
        if native_session_id != "fake-native-s1":
            raise RuntimeStartError(f"unknown native session: {native_session_id}")
        self._move(LifecycleState.STARTING)
        self._native_session_id = native_session_id
        self._session_id = "s1"
        self._move(LifecycleState.IDLE)
        self._emit(RuntimeEventKind.LIFECYCLE, {"state": "resumed"})
        return self._info()

    def _require_running(self) -> None:
        if self._state in (LifecycleState.CLOSED, LifecycleState.FAILED):
            raise RuntimeClosedError(f"runtime is {self._state.value}")
        if self._state is not LifecycleState.IDLE:
            raise RuntimeNotActiveError(f"prompt needs IDLE, state={self._state.value}")

    async def prompt(self, text: str) -> int:
        self._require_running()
        if not text:
            raise RuntimeNotActiveError("prompt text must not be empty")
        self._move(LifecycleState.RUNNING)
        self._turn += 1
        scenario = self._scenario
        if scenario is FakeScenario.STREAMING:
            for chunk in ("hel", "lo, ", "world"):
                self._emit(RuntimeEventKind.MESSAGE, {"chunk": chunk})
            self._emit(RuntimeEventKind.LIFECYCLE, {"state": "completed"})
            self._move(LifecycleState.IDLE)
        elif scenario is FakeScenario.APPROVAL:
            self._approval_pending = "apr-1"
            self._emit(
                RuntimeEventKind.APPROVAL_REQUEST,
                {"approval_id": "apr-1", "action": text},
            )
        elif scenario is FakeScenario.MUTATION_ATTEMPT:
            self._emit(RuntimeEventKind.TOOL_CALL, {"tool": "write_file", "path": text})
            self._emit(RuntimeEventKind.LIFECYCLE, {"state": "completed"})
            self._move(LifecycleState.IDLE)
        else:
            self._emit(RuntimeEventKind.MESSAGE, {"text": f"done: {text}"})
            self._emit(RuntimeEventKind.LIFECYCLE, {"state": "completed"})
            self._move(LifecycleState.IDLE)
        return self._turn

    async def poll_events(self, cursor: int) -> tuple[list[RuntimeEvent], int]:
        if self._scenario is FakeScenario.MALFORMED:
            raise RuntimeProtocolError("fake malformed event stream")
        if cursor < 0:
            cursor = 0
        if self._state in (LifecycleState.CLOSED, LifecycleState.FAILED) and not self._events:
            raise RuntimeClosedError("no events remain on a terminated runtime")
        if self._pause_pending:
            self._pause_pending = False
            self._move(LifecycleState.PAUSED_AT_BOUNDARY)
            self._emit(RuntimeEventKind.LIFECYCLE, {"state": "paused_at_boundary"})
        return list(self._events[cursor:]), len(self._events)

    async def pause_at_boundary(self) -> None:
        if self._state in (LifecycleState.CLOSED, LifecycleState.FAILED):
            raise RuntimeClosedError("cannot pause a terminated runtime")
        if self._scenario is FakeScenario.DELAYED_BOUNDARY and self._state is LifecycleState.RUNNING:
            self._pause_pending = True
            return
        if self._state is LifecycleState.RUNNING:
            raise RuntimeNotActiveError("cannot pause mid-turn; wait for a boundary")
        self._move(LifecycleState.PAUSED_AT_BOUNDARY)
        self._emit(RuntimeEventKind.LIFECYCLE, {"state": "paused_at_boundary"})

    async def resume_from_pause(self) -> None:
        self._move(LifecycleState.IDLE)

    async def permission_response(self, *, approval_id: str, allow: bool) -> None:
        if self._approval_pending != approval_id:
            raise RuntimeProtocolError(f"no pending approval {approval_id!r}")
        self._approval_pending = None
        if allow:
            self._emit(RuntimeEventKind.TOOL_RESULT, {"approval_id": approval_id, "ok": True})
            self._emit(RuntimeEventKind.LIFECYCLE, {"state": "completed"})
        else:
            self._emit(
                RuntimeEventKind.ERROR,
                {"approval_id": approval_id, "reason": "denied"},
            )
            self._emit(RuntimeEventKind.LIFECYCLE, {"state": "completed"})
        self._move(LifecycleState.IDLE)

    async def cancel(self, *, reason: str = "") -> None:
        if self._state in (LifecycleState.CLOSED, LifecycleState.FAILED):
            return
        self._move(LifecycleState.CANCELLING)
        self._approval_pending = None
        self._emit(RuntimeEventKind.LIFECYCLE, {"state": "cancelled", "reason": reason})
        self._move(LifecycleState.CLOSED)

    async def close(self) -> None:
        if self._state in (LifecycleState.CLOSED, LifecycleState.FAILED):
            return
        if self._state not in (LifecycleState.IDLE, LifecycleState.PAUSED_AT_BOUNDARY):
            await self.cancel(reason="close")
            return
        self._emit(RuntimeEventKind.LIFECYCLE, {"state": "closed"})
        self._move(LifecycleState.CLOSED)

    def attempt_late_emit(self) -> None:
        """Simulate a runtime producing output after termination. Must be rejected."""
        self._emit(RuntimeEventKind.MESSAGE, {"text": "late"})
