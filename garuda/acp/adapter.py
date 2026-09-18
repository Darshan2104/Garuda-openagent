"""Generic ACP runtime adapter (P0.15, issue #24).

`AcpRuntime` is the `AgentRuntime` over one managed agent subprocess: launch,
version-checked handshake, capability negotiation, prompting with normalized
events, approval replies, cancellation, and close. Vendor-specific manifests
and registry wiring arrive with the P1 adapters; every one of them runs the
shared conformance suite through this class.

Resume reattaches to the live process only. Cross-process resume is adapter
work for later — claiming it here would need wire methods the owned subset
does not have.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from garuda.acp.authority import (
    AgentCapabilities,
    AuthorityMap,
    AuthorityPolicy,
    negotiate,
)
from garuda.acp.client import AcpProcess
from garuda.acp.normalize import AcpNormalizer
from garuda.acp.protocol import AcpCancelledError, AcpError, AcpTimeoutError
from garuda.runtime.events import RuntimeEvent, RuntimeEventKind
from garuda.runtime.protocol import (
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


class AcpRuntime:
    """`AgentRuntime` speaking ACP to one subprocess."""

    def __init__(
        self,
        argv: list[str],
        *,
        runtime_id: str = "acp",
        policy: dict[str, AuthorityPolicy] | None = None,
        extra_env: dict[str, str] | None = None,
    ):
        self._argv = list(argv)
        self._runtime_id = runtime_id
        self._policy = dict(policy or {})
        self._extra_env = dict(extra_env or {})
        self._process: AcpProcess | None = None
        self._normalizer: AcpNormalizer | None = None
        self._authority: AuthorityMap | None = None
        self._state = LifecycleState.DISCOVERED
        self._garuda_session_id = ""
        self._agent_session_id: str | None = None
        self._turn = 0
        self._events: list[RuntimeEvent] = []
        self._pending_approvals: set[str] = set()

    @property
    def runtime_id(self) -> str:
        return self._runtime_id

    @property
    def kind(self) -> RuntimeKind:
        return RuntimeKind.ACP

    @property
    def version(self) -> str:
        from garuda.acp.protocol import ACP_VERSION

        return ACP_VERSION

    @property
    def state(self) -> LifecycleState:
        return self._state

    @property
    def native_session_id(self) -> str | None:
        return self._agent_session_id

    @property
    def authority(self) -> AuthorityMap | None:
        return self._authority

    async def health(self) -> HealthStatus:
        if self._process is None or not self._process.is_running:
            return HealthStatus.UNAVAILABLE
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
                session_id=self._garuda_session_id,
                turn=self._turn,
                seq=len(self._events),
                payload=dict(payload or {}),
            )
        )

    def _absorb(self, normalized: list[RuntimeEvent]) -> None:
        for event in normalized:
            if event.kind is RuntimeEventKind.APPROVAL_REQUEST:
                approval_id = event.payload.get("approval_id", "")
                if approval_id:
                    self._pending_approvals.add(approval_id)
            self._events.append(
                RuntimeEvent(
                    kind=event.kind,
                    session_id=self._garuda_session_id,
                    turn=self._turn,
                    seq=len(self._events),
                    payload=dict(event.payload),
                )
            )

    async def start(self, *, task: str, session_id: str | None = None) -> RuntimeInfo:
        if self._state is not LifecycleState.DISCOVERED:
            raise RuntimeStartError(f"already started (state={self._state.value})")
        if not task:
            raise RuntimeStartError("task must not be empty")
        self._move(LifecycleState.STARTING)
        self._garuda_session_id = session_id or str(uuid.uuid4())
        process = AcpProcess(self._argv, extra_env=self._extra_env)
        try:
            await process.launch()
            handshake = await process.initialize()
            self._authority = negotiate(
                self._policy,
                AgentCapabilities.from_dict(handshake.get("agentCapabilities")),
            )
            self._agent_session_id = await process.session_new()
        except Exception:
            await process.close()
            self._move(LifecycleState.FAILED)
            raise
        self._process = process
        self._normalizer = AcpNormalizer(self._agent_session_id or self._garuda_session_id)
        self._move(LifecycleState.IDLE)
        self._emit(RuntimeEventKind.LIFECYCLE, {"state": "started", "task": task})
        return self._info()

    async def resume(self, *, native_session_id: str) -> RuntimeInfo:
        if self._state is not LifecycleState.DISCOVERED:
            raise RuntimeStartError(f"cannot resume (state={self._state.value})")
        if self._process is None or self._agent_session_id != native_session_id:
            raise RuntimeStartError(
                "cross-process resume is not supported by this adapter yet"
            )
        self._move(LifecycleState.STARTING)
        self._move(LifecycleState.IDLE)
        self._emit(RuntimeEventKind.LIFECYCLE, {"state": "resumed"})
        return self._info()

    async def prompt(self, text: str, *, timeout: float | None = None) -> int:
        if self._process is None or self._normalizer is None:
            raise RuntimeStartError("runtime is not started")
        if self._state in (LifecycleState.CLOSED, LifecycleState.FAILED):
            raise RuntimeClosedError(f"runtime is {self._state.value}")
        if self._state is not LifecycleState.IDLE:
            raise RuntimeNotActiveError(f"prompt needs IDLE, state={self._state.value}")
        if not text:
            raise RuntimeNotActiveError("prompt text must not be empty")
        self._move(LifecycleState.RUNNING)
        self._turn += 1
        self._normalizer.new_turn()
        prompt_task = asyncio.ensure_future(
            self._process.session_prompt(self._agent_session_id or "", text)
        )
        try:
            if timeout is not None:
                result = await asyncio.wait_for(self._drain_until_done(prompt_task), timeout)
            else:
                result = await self._drain_until_done(prompt_task)
        except TimeoutError as exc:
            prompt_task.cancel()
            await self._close_process()
            self._emit(RuntimeEventKind.LIFECYCLE, {"state": "failed", "reason": "timeout"})
            self._move(LifecycleState.FAILED)
            raise AcpTimeoutError("prompt exceeded its deadline") from exc
        except AcpCancelledError:
            self._absorb(self._normalizer.finish("cancelled"))
            self._move(LifecycleState.CANCELLING)
            self._emit(RuntimeEventKind.LIFECYCLE, {"state": "cancelled"})
            self._move(LifecycleState.CLOSED)
            raise
        except AcpError as exc:
            self._absorb(self._normalizer.finish("failed", detail=str(exc)))
            self._move(LifecycleState.FAILED)
            raise
        stop = result.get("stopReason", "end_turn") if isinstance(result, dict) else "end_turn"
        self._absorb(self._normalizer.finish(stop))
        self._move(LifecycleState.IDLE)
        return self._turn

    async def _drain_until_done(self, prompt_task: asyncio.Task) -> Any:
        """Feed notifications as they stream, returning the prompt result."""
        assert self._process is not None and self._normalizer is not None
        while True:
            done, _ = await asyncio.wait({prompt_task}, timeout=0.05)
            for notification in self._process.drain_notifications():
                update = (notification.get("params") or {}).get("update")
                if update:
                    self._absorb(self._normalizer.feed(update))
            if done:
                return prompt_task.result()

    async def _close_process(self) -> None:
        if self._process is not None:
            await self._process.close()

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
        if approval_id not in self._pending_approvals:
            raise RuntimeProtocolError(f"no pending approval {approval_id!r}")
        if self._process is None:
            raise RuntimeStartError("runtime is not started")
        self._pending_approvals.discard(approval_id)
        await self._process.session_approve(
            self._agent_session_id or "", approval_id, allow
        )

    async def cancel(self, *, reason: str = "") -> None:
        if self._state in (LifecycleState.CLOSED, LifecycleState.FAILED):
            return
        if self._process is not None:
            await self._process.session_cancel(self._agent_session_id or "")
        if self._state is LifecycleState.RUNNING:
            # The in-flight prompt owns the transition to CLOSED; notifying
            # above is what unblocks it. Moving here too would race it.
            return
        self._move(LifecycleState.CANCELLING)
        self._emit(RuntimeEventKind.LIFECYCLE, {"state": "cancelled", "reason": reason})
        self._move(LifecycleState.CLOSED)

    async def close(self) -> None:
        if self._state in (LifecycleState.CLOSED, LifecycleState.FAILED):
            if self._process is not None:
                await self._process.close()
            return
        if self._state is LifecycleState.RUNNING:
            await self.cancel(reason="close")
            return
        self._emit(RuntimeEventKind.LIFECYCLE, {"state": "closed"})
        self._move(LifecycleState.CLOSED)
        if self._process is not None:
            await self._process.close()

    def _info(self) -> RuntimeInfo:
        names: set[str] = set()
        if self._authority is not None:
            names = set(self._authority.owners) | set(self._authority.to_snapshot())
        return RuntimeInfo(
            runtime_id=self._runtime_id,
            kind=self.kind,
            version=self.version,
            capabilities=RuntimeCapabilities(names=frozenset(names)),
            health=HealthStatus.OK,
            auth=AuthStatus.AUTHENTICATED,
            native_session_id=self._agent_session_id,
        )
