"""Generic ACP runtime adapter (P0.15, issue #24).

`AcpRuntime` is the `AgentRuntime` over one managed agent subprocess: launch,
version-checked handshake, capability negotiation, prompting with normalized
events, approval replies, cancellation, and close.

Approvals follow ACP v1: the agent sends a `session/request_permission`
*request* carrying options; the adapter emits an `APPROVAL_REQUEST` event and
answers exactly once — via an injected `approval_handler`, or later through
`permission_response` — by selecting an `allow_once` / `reject_*` option.
Allow never widens to `allow_always`: without an `allow_once` option the
answer is `cancelled`. Any other agent-to-client method is refused with a
JSON-RPC error, because Garuda advertises no `fs` or `terminal` capability. Vendor-specific manifests
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
import logging
import os
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from garuda.acp.authority import (
    AgentCapabilities,
    AuthorityMap,
    AuthorityPolicy,
    negotiate,
)
from garuda.acp.client import AcpProcess
from garuda.acp.normalize import AcpNormalizer
from garuda.acp.protocol import (
    AcpCancelledError,
    AcpError,
    AcpProtocolError,
    AcpTimeoutError,
)
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

logger = logging.getLogger(__name__)

#: JSON-RPC error codes used when refusing agent-initiated requests.
_METHOD_NOT_FOUND = -32601
_INVALID_PARAMS = -32602


class AcpRuntime:
    """`AgentRuntime` speaking ACP to one subprocess."""

    def __init__(
        self,
        argv: list[str],
        *,
        runtime_id: str = "acp",
        cwd: str | None = None,
        policy: dict[str, AuthorityPolicy] | None = None,
        extra_env: dict[str, str] | None = None,
        approval_handler: Callable[[str], Awaitable[bool]] | None = None,
        setup_hint: str = "",
        store=None,
        persist_dir: str | None = None,
        metrics=None,
    ):
        self._argv = list(argv)
        # The agent's session root. `None` keeps the client's historical
        # default (Garuda's own cwd); launch paths pass the workspace.
        if cwd is not None and not os.path.isabs(cwd):
            raise RuntimeStartError("ACP session cwd must be an absolute path")
        self._cwd = cwd
        self._approval_handler = approval_handler
        self._runtime_id = runtime_id
        self._policy = dict(policy or {})
        self._extra_env = dict(extra_env or {})
        self._setup_hint = setup_hint
        self._store = store
        self._recorded_child_pid: int | None = None
        self._persist_dir = persist_dir
        self._metrics = metrics
        self._process: AcpProcess | None = None
        self._normalizer: AcpNormalizer | None = None
        self._authority: AuthorityMap | None = None
        self._state = LifecycleState.DISCOVERED
        self._garuda_session_id = ""
        self._agent_session_id: str | None = None
        self._turn = 0
        self._events: list[RuntimeEvent] = []
        # approval_id -> (JSON-RPC request id, offered options)
        self._pending_approvals: dict[str, tuple[Any, list[dict[str, Any]]]] = {}
        self._answer_tasks: set[asyncio.Task] = set()
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
            if self._store is None:
                # No store means no persisted child record: a Garuda crash
                # would leave this child unrecoverable by `recover()`.
                logger.warning(
                    "ACP runtime %s started without a session store; its child "
                    "process is not recorded for restart recovery",
                    self._runtime_id,
                )
            else:
                self._persist_identity(process)
        except AcpProtocolError as exc:
            self._note_error(exc)
            await process.close()
            self._move(LifecycleState.FAILED)
            if "version mismatch" in str(exc) and self._setup_hint:
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

    def _persist_identity(self, process: AcpProcess) -> None:
        """Persist the active segment and the child record together.

        The segment (runtime id, agent session id, authority snapshot) and the
        child identity are only meaningful as a pair; `recover()` refuses a
        child whose runtime is not bound to the session.
        """
        from garuda.runtime.recovery import record_child
        from garuda.runtime.session import RuntimeSegment

        assert self._store is not None and self._authority is not None
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
            self._store.update_active_runtime_segment(self._garuda_session_id, segment)
        else:
            # Direct ACP execution may join an existing native tenure. A
            # transactional handoff binds only after ownership already moved,
            # so it continues through the replacement branch above.
            self._store.attach_runtime_segment(self._garuda_session_id, segment)
        record_child(
            self._store,
            self._garuda_session_id,
            runtime_id=self._runtime_id,
            pid=process.pid,
            process_group=process.pid,
        )
        self._recorded_child_pid = process.pid

    def bind_session(self, store) -> None:
        """Bind a started, store-less runtime to its session once it owns it.

        A handoff target starts before ownership moves, while the session's
        active segment is still the source, so it cannot record itself at
        `start`. After the acknowledgement makes it the active segment this
        persists its authority snapshot and child record exactly as a
        store-backed `start` would, and a later close retires the record.
        """
        if self._store is not None:
            raise RuntimeStartError("runtime is already bound to a session store")
        if self._process is None or self._state in (
            LifecycleState.CLOSED,
            LifecycleState.FAILED,
        ):
            raise RuntimeStartError("only a started, live runtime can be bound")
        self._store = store
        try:
            self._persist_identity(self._process)
        except Exception:
            self._store = None
            raise

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
        # Open the normalizer turn before RUNNING: if it refuses, the runtime
        # is not left mid-turn with nothing driving it.
        self._normalizer.new_turn()
        self._move(LifecycleState.RUNNING)
        self._turn += 1
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
            self._finish_turn("cancelled")
            await self._close_process()
            self._move(LifecycleState.CANCELLING)
            self._emit(RuntimeEventKind.LIFECYCLE, {"state": "cancelled"})
            self._move(LifecycleState.CLOSED)
            raise
        except AcpError as exc:
            self._note_error(exc)
            self._finish_turn("failed", detail=str(exc))
            await self._close_process()
            self._move(LifecycleState.FAILED)
            raise
        stop = result.get("stopReason") if isinstance(result, dict) else None
        try:
            if not isinstance(stop, str):
                raise AcpProtocolError(f"session/prompt returned no stopReason: {result!r}")
            closing = self._normalizer.finish(stop)
        except AcpProtocolError as exc:
            # An unknown stop reason or an orphaned tool_call_update means the
            # trail cannot be closed truthfully: fail and reap, never stay
            # RUNNING with a live process.
            self._emit(RuntimeEventKind.ERROR, {"message": str(exc)})
            await self._close_process()
            self._move(LifecycleState.FAILED)
            raise
        self._absorb(closing)
        if stop == "cancelled":
            # The agent ended the turn cancelled (e.g. its permission request
            # was answered `cancelled`). The normalizer has closed the
            # session, so the runtime closes too instead of idling on a trail
            # that can take no further turn.
            await self._close_process()
            self._move(LifecycleState.CANCELLING)
            self._move(LifecycleState.CLOSED)
            raise AcpCancelledError("agent ended the turn cancelled")
        self._move(LifecycleState.IDLE)
        return self._turn

    def _finish_turn(self, reason: str, *, detail: str = "") -> None:
        """Close the normalizer turn on a path that must still reap.

        A trail that cannot close cleanly (updates for unknown tool calls) is
        recorded as an error event rather than raised past the cleanup.
        """
        assert self._normalizer is not None
        try:
            self._absorb(self._normalizer.finish(reason, detail=detail))
        except AcpProtocolError as exc:
            self._emit(RuntimeEventKind.ERROR, {"message": str(exc)})

    async def _drain_until_done(self, prompt_task: asyncio.Task) -> Any:
        """Feed notifications as they stream, returning the prompt result."""
        assert self._process is not None and self._normalizer is not None
        while True:
            done, _ = await asyncio.wait({prompt_task}, timeout=0.05)
            for notification in self._process.drain_notifications():
                if notification.get("method") != "session/update":
                    continue
                update = (notification.get("params") or {}).get("update")
                if update:
                    self._absorb(self._normalizer.feed(update))
            for request in self._process.drain_requests():
                await self._on_agent_request(request)
            if done:
                return prompt_task.result()

    async def _on_agent_request(self, request: dict[str, Any]) -> None:
        """Answer one agent-initiated request, or park it as an approval."""
        assert self._process is not None
        method = request.get("method")
        request_id = request.get("id")
        if method != "session/request_permission":
            await self._process.respond(
                request_id,
                error={
                    "code": _METHOD_NOT_FOUND,
                    "message": f"client does not support {method!r}",
                },
            )
            return
        params = request.get("params") or {}
        tool_call = params.get("toolCall") if isinstance(params, dict) else None
        options = params.get("options") if isinstance(params, dict) else None
        # Typed so JSON-RPC ids 1 and "1" cannot collide.
        approval_id = f"perm-{type(request_id).__name__}-{request_id}"
        if (
            not isinstance(tool_call, dict)
            or not isinstance(options, list)
            or not options
            or any(not isinstance(option, dict) for option in options)
            or approval_id in self._pending_approvals
        ):
            await self._process.respond(
                request_id,
                error={"code": _INVALID_PARAMS, "message": "malformed permission request"},
            )
            return
        self._pending_approvals[approval_id] = (request_id, options)
        action = str(tool_call.get("title") or tool_call.get("toolCallId") or "permission")
        self._emit(
            RuntimeEventKind.APPROVAL_REQUEST,
            {
                "approval_id": approval_id,
                "action": action,
                "tool_call_id": tool_call.get("toolCallId", ""),
                "options": [
                    {
                        "option_id": option.get("optionId", ""),
                        "kind": option.get("kind", ""),
                        "name": option.get("name", ""),
                    }
                    for option in options
                ],
            },
        )
        if self._approval_handler is not None:
            task = asyncio.ensure_future(self._answer_with_handler(approval_id, action))
            self._answer_tasks.add(task)
            task.add_done_callback(self._answer_tasks.discard)

    async def _answer_with_handler(self, approval_id: str, action: str) -> None:
        assert self._approval_handler is not None
        try:
            allowed = bool(await self._approval_handler(action))
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("ACP approval handler failed for %s; denying", approval_id, exc_info=True)
            allowed = False
        try:
            await self.permission_response(approval_id=approval_id, allow=allowed)
        except (RuntimeProtocolError, RuntimeStartError, AcpError):
            # Already answered, cancelled, or the process is gone.
            pass

    async def _cancel_pending_approvals(self) -> None:
        """v1 requires every open permission request to be answered
        `cancelled` when its turn is cancelled."""
        for task in list(self._answer_tasks):
            task.cancel()
        pending, self._pending_approvals = self._pending_approvals, {}
        if self._process is None:
            return
        for request_id, _options in pending.values():
            try:
                await self._process.respond(
                    request_id, result={"outcome": {"outcome": "cancelled"}}
                )
            except AcpError:
                pass

    async def _close_process(self) -> None:
        # No interactive answerer outlives the process it would answer.
        for task in list(self._answer_tasks):
            task.cancel()
        if self._process is not None:
            await self._process.close()
        self._mark_child_exited()

    def _mark_child_exited(self) -> None:
        """Retire the recorded child once `AcpProcess.close` has reaped it.

        A record left `live` after a clean exit makes a later recovery probe
        whatever process reuses that PID. A crash between `close` and this
        write still leaves it `live`; recovery's persisted start-time/command
        identity check is what keeps that record from signalling a stranger.
        """
        pid, self._recorded_child_pid = self._recorded_child_pid, None
        if pid is None or self._store is None:
            return
        from garuda.runtime.recovery import record_child_exit

        try:
            record_child_exit(self._store, self._garuda_session_id, pid=pid)
        except Exception:
            logger.warning("Could not retire ACP child %s", pid, exc_info=True)

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
        if self._process is None:
            raise RuntimeStartError("runtime is not started")
        entry = self._pending_approvals.pop(approval_id, None)
        if entry is None:
            raise RuntimeProtocolError(f"no pending approval {approval_id!r}")
        request_id, options = entry
        option_id = _pick_option(options, allow)
        outcome = (
            {"outcome": "selected", "optionId": option_id}
            if option_id is not None
            else {"outcome": "cancelled"}
        )
        with self._timed("approval"):
            await self._process.respond(request_id, result={"outcome": outcome})

    async def cancel(self, *, reason: str = "") -> None:
        if self._state in (LifecycleState.CLOSED, LifecycleState.FAILED):
            return
        # The audit is best-effort *before* cancelling and surfaced *after*:
        # a persistence failure must never keep the agent running.
        audit_error: Exception | None = None
        if self._store is not None:
            from garuda.runtime.recovery import record_cancel

            try:
                record_cancel(
                    self._store,
                    self._garuda_session_id,
                    boundary="process",
                    reason=reason or "cancelled",
                )
            except Exception as exc:
                logger.warning("Could not persist ACP cancellation", exc_info=True)
                audit_error = exc
        await self._cancel_pending_approvals()
        if self._process is not None:
            await self._process.session_cancel(self._agent_session_id or "")
        if self._state is not LifecycleState.RUNNING:
            # While RUNNING the in-flight prompt owns the transition to
            # CLOSED; notifying above is what unblocks it.
            self._move(LifecycleState.CANCELLING)
            self._emit(RuntimeEventKind.LIFECYCLE, {"state": "cancelled", "reason": reason})
            self._move(LifecycleState.CLOSED)
        if audit_error is not None:
            from garuda.runtime.recovery import CancellationAuditError

            raise CancellationAuditError(
                f"ACP runtime cancelled but its audit record failed: {audit_error}"
            ) from audit_error

    async def close(self) -> None:
        if self._state in (LifecycleState.CLOSED, LifecycleState.FAILED):
            await self._close_process()
            return
        if self._state is LifecycleState.RUNNING:
            try:
                await self.cancel(reason="close")
            finally:
                # A cancel audit failure must not leave the child running.
                await self._close_process()
            return
        self._emit(RuntimeEventKind.LIFECYCLE, {"state": "closed"})
        self._move(LifecycleState.CLOSED)
        with self._timed("cleanup"):
            await self._close_process()

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


def _pick_option(options: list[dict[str, Any]], allow: bool) -> str | None:
    """The narrowest option matching the decision, or None (answer cancelled)."""
    kinds = ("allow_once",) if allow else ("reject_once", "reject_always")
    for kind in kinds:
        for option in options:
            option_id = option.get("optionId")
            if option.get("kind") == kind and isinstance(option_id, str) and option_id:
                return option_id
    return None
