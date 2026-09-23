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
import contextlib
import json
import uuid
from pathlib import Path
from typing import Any

from garuda.acp.authority import (
    AgentCapabilities,
    AuthorityMap,
    AuthorityPolicy,
    negotiate,
)
from garuda.acp.client import AcpProcess, ClientRequestHandler
from garuda.acp.normalize import AcpNormalizer
from garuda.acp.protocol import AcpCancelledError, AcpError, AcpProtocolError, AcpTimeoutError
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
        client_request_handler: ClientRequestHandler | None = None,
        setup_hint: str = "",
        store=None,
        persist_dir: str | None = None,
        cwd: str | None = None,
        metrics=None,
    ):
        from garuda.observability.runtime_metrics import RuntimeMetrics

        self._argv = list(argv)
        self._runtime_id = runtime_id
        self._policy = dict(policy or {})
        self._extra_env = dict(extra_env or {})
        self._client_request_handler = client_request_handler
        self._setup_hint = setup_hint
        self._store = store
        self._persist_dir = persist_dir
        self._cwd = cwd
        # Always-on: every adapter run records its own timings and triage,
        # so metrics are observable in real runs without opt-in plumbing.
        # Callers (mostly tests) may inject their own recorder instead.
        self._metrics = (
            metrics if metrics is not None else RuntimeMetrics(adapter_version=self.version)
        )
        self._process: AcpProcess | None = None
        self._normalizer: AcpNormalizer | None = None
        self._authority: AuthorityMap | None = None
        self._state = LifecycleState.DISCOVERED
        self._garuda_session_id = ""
        self._agent_session_id: str | None = None
        self._turn = 0
        self._events: list[RuntimeEvent] = []
        self._pending_approvals: set[str] = set()
        self._quota: dict[str, Any] | None = None

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

    @property
    def quota(self) -> dict[str, Any] | None:
        """Harness-supplied quota, passed through untouched. None means unknown —
        never estimated, never zero-filled."""
        return dict(self._quota) if self._quota is not None else None

    @property
    def metrics(self):
        """The attached metrics recorder, if any."""
        return self._metrics

    def _timed(self, phase: str):
        if self._metrics is None:
            return contextlib.nullcontext()
        return self._metrics.timed(phase)

    def _note_error(self, exc: BaseException) -> None:
        if self._metrics is not None:
            self._metrics.note_error(exc)

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
            self._persist(self._events[-1])

    def _persist(self, event: RuntimeEvent) -> None:
        """Append normalized external events with their immutable ACP session id."""
        if self._persist_dir is None:
            return
        try:
            path = Path(self._persist_dir) / "acp-events.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(
                        {
                            "segment_id": self._agent_session_id or self._garuda_session_id,
                            "kind": event.kind.value,
                            "session_id": event.session_id,
                            "turn": event.turn,
                            "seq": event.seq,
                        },
                        default=str,
                    )
                    + "\n"
                )
        except OSError:
            # Observability must not turn a running external agent into a failure.
            return

    async def start(self, *, task: str, session_id: str | None = None) -> RuntimeInfo:
        if self._state is not LifecycleState.DISCOVERED:
            raise RuntimeStartError(f"already started (state={self._state.value})")
        if not task:
            raise RuntimeStartError("task must not be empty")
        self._move(LifecycleState.STARTING)
        self._garuda_session_id = session_id or str(uuid.uuid4())
        process = AcpProcess(
            self._argv,
            extra_env=self._extra_env,
            client_request_handler=self._client_request_handler,
            cwd=self._cwd,
        )
        try:
            with self._timed("startup"):
                await process.launch()
                handshake = await process.initialize()
            with self._timed("negotiation"):
                self._authority = negotiate(
                    self._policy,
                    AgentCapabilities.from_dict(handshake.get("agentCapabilities")),
                )
            quota = handshake.get("quota")
            self._quota = dict(quota) if isinstance(quota, dict) else None
            self._agent_session_id = await process.session_new(cwd=self._cwd)
            if self._store is not None:
                from garuda.runtime.recovery import record_child
                from garuda.runtime.session import RuntimeSegment

                # ACP launches with start_new_session, so its PID is also the
                # isolated process-group leader recovery may safely signal.
                if process.pid is None:
                    raise RuntimeStartError("ACP process launched without a pid")
                segment = RuntimeSegment(
                    runtime_id=self._runtime_id,
                    kind=self.kind.value,
                    native_session_id=self._agent_session_id,
                    version=self.version,
                    capabilities=self._authority.to_snapshot(),
                )
                unified = self._store.load_unified(self._garuda_session_id)
                if unified.active.runtime_id == self._runtime_id:
                    self._store.update_active_runtime_segment(
                        self._garuda_session_id, segment
                    )
                else:
                    self._store.attach_runtime_segment(self._garuda_session_id, segment)
                record_child(
                    self._store,
                    self._garuda_session_id,
                    runtime_id=self._runtime_id,
                    pid=process.pid,
                    process_group=process.pid,
                )
        except AcpProtocolError as exc:
            self._note_error(exc)
            await process.close()
            self._move(LifecycleState.FAILED)
            if "speaks ACP" in str(exc) and self._setup_hint:
                raise AcpProtocolError(f"{exc} Upgrade the adapter: {self._setup_hint}") from exc
            raise
        except Exception as exc:
            self._note_error(exc)
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
            with self._timed("turn"):
                if timeout is not None:
                    result = await asyncio.wait_for(self._drain_until_done(prompt_task), timeout)
                else:
                    result = await self._drain_until_done(prompt_task)
        except TimeoutError as exc:
            prompt_task.cancel()
            await self._close_process()
            self._emit(RuntimeEventKind.LIFECYCLE, {"state": "failed", "reason": "timeout"})
            self._move(LifecycleState.FAILED)
            timeout_error = AcpTimeoutError("prompt exceeded its deadline")
            self._note_error(timeout_error)
            raise timeout_error from exc
        except AcpCancelledError:
            self._absorb(self._normalizer.finish("cancelled"))
            self._move(LifecycleState.CANCELLING)
            self._emit(RuntimeEventKind.LIFECYCLE, {"state": "cancelled"})
            self._move(LifecycleState.CLOSED)
            raise
        except AcpError as exc:
            self._note_error(exc)
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
        with self._timed("approval"):
            await self._process.session_approve(
                self._agent_session_id or "", approval_id, allow
            )

    async def cancel(self, *, reason: str = "") -> None:
        if self._state in (LifecycleState.CLOSED, LifecycleState.FAILED):
            return
        if self._store is not None:
            from garuda.runtime.recovery import record_cancel

            record_cancel(
                self._store,
                self._garuda_session_id,
                boundary="process",
                reason=reason or "cancelled",
            )
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
            self._persist_metrics()
            return
        if self._state is LifecycleState.RUNNING:
            await self.cancel(reason="close")
            return
        self._emit(RuntimeEventKind.LIFECYCLE, {"state": "closed"})
        self._move(LifecycleState.CLOSED)
        if self._process is not None:
            with self._timed("cleanup"):
                await self._process.close()
        self._persist_metrics()

    def _persist_metrics(self) -> None:
        """Write the metrics snapshot beside the session trail, best-effort.

        A read-only session dir must never fail teardown; without a persist
        dir the snapshot simply lives on `self.metrics` for the caller.
        """
        if self._persist_dir is None or self._metrics is None:
            return
        try:
            import json as _json

            path = Path(self._persist_dir) / "metrics.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                _json.dumps(self._metrics.to_dict(), indent=2), encoding="utf-8"
            )
        except OSError:
            pass

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
