"""`NativeGarudaRuntime`: the native loop behind the `AgentRuntime` boundary (#14).

Task semantics are unchanged — a `prompt` runs the injected agent to completion
exactly as `run_agent_task` always has. The bridge owns only the boundary: the
unified session record, lifecycle states, the normalized event log, and parked
approval answers. Entry points keep resolving environments and tearing them
down; that code path is untouched.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from garuda import __version__ as _GARUDA_VERSION
from garuda.core.events import EventStore, EventType
from garuda.core.sessions import SessionStore
from garuda.runtime.events import RuntimeEvent, RuntimeEventKind
from garuda.runtime.protocol import (
    AuthStatus,
    HealthStatus,
    LifecycleState,
    RuntimeCancelledError,
    RuntimeCapabilities,
    RuntimeClosedError,
    RuntimeInfo,
    RuntimeKind,
    RuntimeNotActiveError,
    RuntimeProtocolError,
    RuntimeStartError,
    check_transition,
)

#: Native event types mapped onto the normalized vocabulary. Anything absent
#: renders as a MESSAGE carrying the original type — lossy but explicit, and
#: the raw trail stays in the session's events.jsonl either way.
_EVENT_KIND_MAP = {
    EventType.USER_MESSAGE: RuntimeEventKind.MESSAGE,
    EventType.MODEL_RESPONSE: RuntimeEventKind.MESSAGE,
    EventType.TOOL_CALL: RuntimeEventKind.TOOL_CALL,
    EventType.TOOL_RESULT: RuntimeEventKind.TOOL_RESULT,
    EventType.PERMISSION_ASK: RuntimeEventKind.APPROVAL_REQUEST,
    EventType.VERIFICATION: RuntimeEventKind.TOOL_RESULT,
    EventType.SESSION_START: RuntimeEventKind.LIFECYCLE,
    EventType.SESSION_END: RuntimeEventKind.LIFECYCLE,
    EventType.BUDGET: RuntimeEventKind.MESSAGE,
    EventType.CONTRACT: RuntimeEventKind.MESSAGE,
    EventType.TURN_METRICS: RuntimeEventKind.MESSAGE,
    EventType.SIDE_EFFECTS: RuntimeEventKind.MESSAGE,
    EventType.ENVIRONMENT_SNAPSHOT: RuntimeEventKind.MESSAGE,
    EventType.ENVIRONMENT_UNAVAILABLE: RuntimeEventKind.ERROR,
    EventType.SUMMARIZATION: RuntimeEventKind.MESSAGE,
}


class NativeGarudaRuntime:
    """`AgentRuntime` over `DefaultAgent`/`RigorousAgent` execution."""

    def __init__(
        self,
        *,
        agent,
        model,
        tools: list,
        config,
        permissions,
        store: SessionStore | None = None,
        events: EventStore | None = None,
        run: Callable[..., Awaitable[Any]] | None = None,
    ):
        self._agent = agent
        self._model = model
        self._tools = tools
        self._config = config
        self._permissions = permissions
        self._store = store or SessionStore()
        self._provided_trail = events
        self._trail: EventStore | None = None
        self._run = run
        self._state = LifecycleState.DISCOVERED
        self._session_id = ""
        self._turn = 0
        self._events: list[RuntimeEvent] = []
        self._cancel_requested: str | None = None
        self._parked_approvals: dict[str, Callable[[bool], None]] = {}
        self._last_result: Any = None

    @property
    def last_result(self) -> Any:
        """The `AgentResult` of the most recent prompt, or None before the first."""
        return self._last_result

    def install_driver(self, driver: Callable[..., Awaitable[Any]]) -> None:
        """Provide the execution driver. Entry points only; fails if replaced."""
        if self._run is not None:
            raise RuntimeStartError("a run driver is already installed")
        self._run = driver

    @property
    def runtime_id(self) -> str:
        return "native"

    @property
    def kind(self) -> RuntimeKind:
        return RuntimeKind.NATIVE

    @property
    def version(self) -> str:
        return _GARUDA_VERSION

    @property
    def state(self) -> LifecycleState:
        return self._state

    @property
    def native_session_id(self) -> str | None:
        return self._session_id or None

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

    def _normalize_trail(self, trail: EventStore, *, skip: int = 0) -> None:
        for event in trail.get_all()[skip:]:
            try:
                native_kind = EventType(event.get("type", ""))
            except ValueError:
                native_kind = None
            kind = _EVENT_KIND_MAP.get(native_kind, RuntimeEventKind.MESSAGE)
            payload = dict(event.get("payload", {}))
            payload.setdefault("native_type", event.get("type"))
            turn = payload.get("turn", self._turn)
            self._events.append(
                RuntimeEvent(
                    kind=kind,
                    session_id=self._session_id,
                    turn=turn if isinstance(turn, int) and turn >= 0 else self._turn,
                    seq=len(self._events),
                    payload=payload,
                )
            )

    def _info(self) -> RuntimeInfo:
        return RuntimeInfo(
            runtime_id=self.runtime_id,
            kind=self.kind,
            version=self.version,
            capabilities=RuntimeCapabilities(names=frozenset({"prompt", "cancel", "resume"})),
            health=HealthStatus.OK,
            auth=AuthStatus.AUTHENTICATED,
            native_session_id=self.native_session_id,
        )

    def park_approval(self, approval_id: str, respond: Callable[[bool], None]) -> None:
        """Park a native approval so `permission_response` can answer it later.

        Entry points install this as their approval handler; without one,
        approvals flow exactly as they do today and a reply fails closed.
        """
        self._parked_approvals[approval_id] = respond

    async def start(self, *, task: str, session_id: str | None = None) -> RuntimeInfo:
        if self._state is not LifecycleState.DISCOVERED:
            raise RuntimeStartError(f"already started (state={self._state.value})")
        if not task:
            raise RuntimeStartError("task must not be empty")
        self._move(LifecycleState.STARTING)
        if session_id is None and self._provided_trail is not None:
            session_id = self._provided_trail.session_id
        self._session_id = session_id or EventStore().session_id
        self._trail = self._provided_trail or EventStore(session_id=self._session_id)
        self._store.begin(
            session_id=self._session_id,
            task=task,
            model=getattr(self._model, "model_name", str(self._model)),
            agent=getattr(self._agent, "profile_name", "agent"),
            workspace=str(getattr(self._config, "workspace", ".")),
        )
        self._store.ensure_unified(self._session_id)
        self._move(LifecycleState.IDLE)
        self._emit(RuntimeEventKind.LIFECYCLE, {"state": "started", "task": task})
        return self._info()

    async def resume(self, *, native_session_id: str) -> RuntimeInfo:
        if self._state is not LifecycleState.DISCOVERED:
            raise RuntimeStartError(f"cannot resume (state={self._state.value})")
        resolved = self._store.resolve(native_session_id)
        # Classify first: a prepared-but-unacknowledged switch rolls back, and
        # a malformed or ambiguous trail refuses the resume outright.
        try:
            from garuda.runtime.recovery import RecoveryError, recover

            recover(self._store, resolved)
        except RecoveryError as exc:
            raise RuntimeStartError(f"cannot resume session {resolved}: {exc}") from exc
        self._move(LifecycleState.STARTING)
        self._session_id = resolved
        unified = self._store.ensure_unified(resolved)
        if not any(s.runtime_id == "native" for s in unified.segments):
            raise RuntimeStartError(f"session {resolved} has no native segment")
        # Rebind the prior trail so polling replays history, not just new turns.
        self._trail = EventStore(session_id=resolved)
        events_file = self._store.events_path(resolved)
        if events_file.exists():
            self._trail = EventStore.load(events_file)
        self._turn = max(
            (e.get("payload", {}).get("turn", 0) for e in self._trail.get_all()),
            default=0,
        )
        self._move(LifecycleState.IDLE)
        self._emit(RuntimeEventKind.LIFECYCLE, {"state": "resumed"})
        return self._info()

    async def prompt(self, text: str) -> int:
        if self._state in (LifecycleState.CLOSED, LifecycleState.FAILED):
            raise RuntimeClosedError(f"runtime is {self._state.value}")
        if self._state is not LifecycleState.IDLE:
            raise RuntimeNotActiveError(f"prompt needs IDLE, state={self._state.value}")
        if not text:
            raise RuntimeNotActiveError("prompt text must not be empty")
        if self._cancel_requested is not None:
            reason = self._cancel_requested
            self._cancel_requested = None
            self._move(LifecycleState.CANCELLING)
            self._emit(RuntimeEventKind.LIFECYCLE, {"state": "cancelled", "reason": reason})
            self._move(LifecycleState.CLOSED)
            raise RuntimeCancelledError("cancelled before the turn started")
        if self._run is None:
            raise RuntimeStartError("no run driver installed; entry points provide it")
        self._move(LifecycleState.RUNNING)
        self._turn += 1
        assert self._trail is not None
        seen = len(self._trail.get_all())
        try:
            result = await self._run(task=text, turn=self._turn, trail=self._trail)
        except Exception:
            # Emit while RUNNING: terminal states reject all later emission.
            self._emit(RuntimeEventKind.LIFECYCLE, {"state": "failed"})
            self._move(LifecycleState.FAILED)
            raise
        self._last_result = result
        self._normalize_trail(self._trail, skip=seen)
        self._store.advance_event_cursor(self._session_id, len(self._events))
        if self._cancel_requested is not None:
            reason = self._cancel_requested
            self._cancel_requested = None
            self._move(LifecycleState.CANCELLING)
            self._emit(RuntimeEventKind.LIFECYCLE, {"state": "cancelled", "reason": reason})
            self._move(LifecycleState.CLOSED)
            raise RuntimeCancelledError("cancelled at the turn boundary")
        self._emit(RuntimeEventKind.LIFECYCLE, {"state": "completed", "success": bool(result)})
        self._move(LifecycleState.IDLE)
        return self._turn

    async def poll_events(self, cursor: int) -> tuple[list[RuntimeEvent], int]:
        if cursor < 0:
            cursor = 0
        return list(self._events[cursor:]), len(self._events)

    async def pause_at_boundary(self) -> None:
        if self._state in (LifecycleState.CLOSED, LifecycleState.FAILED):
            raise RuntimeClosedError("cannot pause a terminated runtime")
        if self._state is LifecycleState.RUNNING:
            raise RuntimeNotActiveError("cannot pause mid-turn; wait for a boundary")
        self._move(LifecycleState.PAUSED_AT_BOUNDARY)
        self._emit(RuntimeEventKind.LIFECYCLE, {"state": "paused_at_boundary"})

    async def resume_from_pause(self) -> None:
        self._move(LifecycleState.IDLE)

    async def permission_response(self, *, approval_id: str, allow: bool) -> None:
        respond = self._parked_approvals.pop(approval_id, None)
        if respond is None:
            raise RuntimeProtocolError(f"no pending approval {approval_id!r}")
        respond(allow)

    async def cancel(self, *, reason: str = "") -> None:
        if self._state in (LifecycleState.CLOSED, LifecycleState.FAILED):
            return
        if self._state is LifecycleState.RUNNING:
            self._cancel_requested = reason or "cancelled"
            return
        self._move(LifecycleState.CANCELLING)
        self._emit(RuntimeEventKind.LIFECYCLE, {"state": "cancelled", "reason": reason})
        self._move(LifecycleState.CLOSED)

    async def close(self) -> None:
        if self._state in (LifecycleState.CLOSED, LifecycleState.FAILED):
            return
        if self._state is LifecycleState.RUNNING:
            await self.cancel(reason="close")
            return
        self._emit(RuntimeEventKind.LIFECYCLE, {"state": "closed"})
        self._move(LifecycleState.CLOSED)
