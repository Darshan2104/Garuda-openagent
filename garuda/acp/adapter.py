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

Resume reattaches to the live process, or in a new process reloads the
agent's session with `session/load` — attempted only for adapters whose load
was exercised (`runtime.resume.PROVEN_LOAD`); otherwise sessions continue
through a brief (B.7).

A session in the harness's ``plan`` mode never leaves it (#771). The agent
ends planning by asking to switch mode (Claude's ExitPlanMode, "Approve
Plan", ``kind=switch_mode``); Garuda refuses that request itself, whatever the
approval handler would answer, and the plan it carries becomes the turn's
reply: the agent's interrupted (``cancelled``) turn closes ``completed`` with
the plan as its message.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import time
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
from garuda.acp.normalize import AcpNormalizer, normalize_tool_kind
from garuda.acp.protocol import (
    AcpCancelledError,
    AcpError,
    AcpProtocolError,
    AcpSilenceError,
    AcpTimeoutError,
    AcpUsageLimitError,
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


class ResumeUnavailable(RuntimeStartError):
    """The agent cannot reload the session here; continue through a brief."""

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
        confined_approval_handler: Callable[[str, str, bool], tuple[bool, str]] | None = None,
        setup_hint: str = "",
        store=None,
        persist_dir: str | None = None,
        metrics=None,
        agent_definition: dict | None = None,
        handshake_timeout: float | None = None,
        handshake_hint: str = "",
        permission: str = "smart",
        declared_modes: dict[str, str] | None = None,
    ):
        from garuda.observability.runtime_metrics import RuntimeMetrics

        self._argv = list(argv)
        # The agent's session root. `None` keeps the client's historical
        # default (Garuda's own cwd); launch paths pass the workspace.
        if cwd is not None and not os.path.isabs(cwd):
            raise RuntimeStartError("ACP session cwd must be an absolute path")
        self._cwd = cwd
        self._approval_handler = approval_handler
        self._confined_approval_handler = confined_approval_handler
        self._runtime_id = runtime_id
        # The most a prompt that no run path secured may do (:meth:`secure_mode`).
        self._permission = permission
        # The trusted manifest's classification of this adapter's modes, used only
        # where Garuda has no table for the runtime (#732).
        self._declared_modes = dict(declared_modes) if declared_modes else None
        definition = agent_definition or {}
        self._agent_segment = {
            "id": str(uuid.uuid4()), "name": definition.get("name") or runtime_id,
            "digest": definition.get("digest"), "runtime": runtime_id,
            "kind": "acp_execution",
        }
        self._policy = dict(policy or {})
        self._extra_env = dict(extra_env or {})
        self._setup_hint = setup_hint
        # The startup (handshake) deadline; ``None`` keeps the client default.
        self._handshake_timeout = handshake_timeout
        self._handshake_hint = handshake_hint
        self._store = store
        self._recorded_child_pid: int | None = None
        # Handoff targets start store-less; a `PreparedChildRecorder` lets
        # them record their child at launch, before ownership moves.
        self._prepared_recorder = None
        self._prepared_child_pid: int | None = None
        self._persist_dir = persist_dir
        # The user's opt-in study-trace level (#691); only a persisting run reads it.
        self._trace_detail = "metadata"
        if persist_dir is not None:
            from garuda.observability.trace_detail import configured_level

            self._trace_detail = configured_level()
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
        # approval_id -> (JSON-RPC request id, offered options, action)
        self._pending_approvals: dict[str, tuple[Any, list[dict[str, Any]], str]] = {}
        self._answer_tasks: set[asyncio.Task] = set()
        self._quota: dict[str, Any] | None = None
        self._consult_host = None
        self._agent_info: dict[str, Any] = {}
        # The permission mode checked before the first prompt (#700): whether
        # it was checked, what Garuda set, and what the agent then reported.
        self._mode_checked = False
        self._mode_set = False
        self._effective_mode: dict[str, Any] | None = None
        #: Actions denied this turn, named when the agent then ends the turn cancelled.
        self._denied: list[str] = []
        #: The plan of a plan-mode exit Garuda refused this turn (#771): the turn's reply.
        self._plan_result: str | None = None
        #: Whether Garuda cancelled this turn (a plan then never stands for the reply).
        self._cancel_requested = False
        #: What the session request sent (#726): method, cwd and MCP server names only.
        self._session_request: dict[str, Any] | None = None
        #: How the latest turn ended (#824): the harness's raw v1 ``stopReason``
        #: (``None`` when it sent none: Garuda cancelled, timed out or it failed)
        #: and whether Garuda denied an action during it.
        self._last_turn: dict[str, Any] | None = None
        #: The opt-in silence watchdog (#828): ``(silence_sec, tool_silence_sec)``,
        #: ``None`` (the default) for every caller that did not ask for one.
        self._silence: tuple[float | None, float | None] | None = None
        #: Tool calls of the current turn that have not completed or failed.
        self._calls_in_flight: set[str] = set()

    def watch_silence(self, *, silence_sec: float | None,
                      tool_silence_sec: float | None) -> None:
        """Stop a turn whose harness sends nothing for too long (#828); flows only.

        Silence is the time since the harness's last notification or request.
        While a permission request is parked it never counts; while a tool call
        is in flight (``tool_call`` / ``tool_call_update`` not yet ``completed``
        or ``failed``) ``tool_silence_sec`` applies instead of ``silence_sec``.
        ``None`` is no limit in that state. When it fires the turn is cancelled
        and the agent stopped as at a prompt deadline, and the prompt raises
        :class:`AcpSilenceError`. Without this call nothing is watched.
        """
        for value in (silence_sec, tool_silence_sec):
            if value is not None and not (isinstance(value, (int, float)) and value > 0):
                raise ValueError("silence limits must be positive seconds or None")
        self._silence = (silence_sec, tool_silence_sec)

    def _silence_limit(self) -> float | None:
        assert self._silence is not None
        if self._pending_approvals:
            return None  # a parked approval waits for a person: never silence
        return self._silence[1] if self._calls_in_flight else self._silence[0]

    def _track_tool_call(self, update: dict[str, Any]) -> None:
        kind = update.get("sessionUpdate")
        call_id = update.get("toolCallId")
        if kind not in ("tool_call", "tool_call_update") or not isinstance(call_id, str):
            return
        status = update.get("status")
        if status in ("completed", "failed"):
            self._calls_in_flight.discard(call_id)
        elif kind == "tool_call" or status in ("pending", "in_progress"):
            self._calls_in_flight.add(call_id)

    def attach_consult(self, host) -> None:
        """Offer the ``consult`` tool to this agent when its adapter identity is proved (G.3)."""
        self._consult_host = host

    async def _consult_servers(self, handshake: dict[str, Any], process) -> list[dict[str, Any]]:
        """The ``mcpServers`` to send: empty unless the host exposes the tool. Any failure
        means no tool; it never fails the session."""
        info = handshake.get("agentInfo")
        self._agent_info = info if isinstance(info, dict) else {}
        for source, target in (("name", "reported_agent"), ("version", "reported_version")):
            if isinstance(self._agent_info.get(source), str):
                self._agent_segment[target] = self._agent_info[source]
        if self._consult_host is None:
            return []
        try:
            return await self._consult_host.mcp_servers(
                self._agent_info, session_id=self._garuda_session_id, pid=process.pid)
        except Exception:
            logger.warning("The consult tool was not offered", exc_info=False)
            return []

    def _note_session_request(self, method: str, cwd: str, servers) -> None:
        """Names only: an MCP server's command, arguments and environment are never kept."""
        names = []
        for server in list(servers or [])[:16]:
            name = server.get("name") if isinstance(server, dict) else None
            names.append(" ".join(name.split())[:128] if isinstance(name, str) else None)
        self._session_request = {"method": method, "cwd": cwd,
                                 "mcp_servers": names, "mcp_server_count": len(servers or [])}

    @property
    def agent_segment(self) -> dict[str, Any]:
        """This run's agent segment as its events carry it: the definition's name and
        digest and the adapter's reported identity (#726)."""
        return {**self._agent_segment, "native_session_id": self._agent_session_id}

    async def _close_consult(self) -> None:
        if self._consult_host is not None:
            await self._consult_host.close()

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
    def reported_adapter_version(self) -> str:
        version = self._agent_info.get("version")
        return version if isinstance(version, str) else "unknown"

    def negotiated_session(self) -> dict[str, Any]:
        """What the agent reported at ``initialize`` and ``session/new`` (#691): its
        identity, the options it offers with their current values, and its modes.

        Bounded agent-reported names only; a study of a run that failed while its
        role was applied can see what was on offer without talking to the agent."""

        def name(value: Any) -> str | None:
            if not isinstance(value, (str, int, float)) or isinstance(value, bool):
                return None
            text = " ".join(str(value).split())
            return text[:128] or None

        record: dict[str, Any] = {"agent_info": {
            key: text for key in ("name", "title", "version")
            if (text := name(self._agent_info.get(key))) is not None}}
        process = self._process
        options = []
        for option in list(getattr(process, "session_config_options", None) or [])[:16]:
            if not isinstance(option, dict):
                continue
            values = [text for value in (option.get("options") or [])[:32]
                      if isinstance(value, dict) and (text := name(value.get("value"))) is not None]
            options.append({"id": name(option.get("id")), "category": name(option.get("category")),
                            "current": name(option.get("currentValue")), "values": values})
        record["config_options"] = options
        if self._session_request is not None:
            record["session_request"] = dict(self._session_request)
        modes = getattr(process, "session_modes", None)
        if isinstance(modes, dict):
            available = modes.get("availableModes")
            record["modes"] = {
                "current": name(modes.get("currentModeId")),
                "available": [text for mode in (available if isinstance(available, list) else [])[:16]
                              if isinstance(mode, dict) and (text := name(mode.get("id"))) is not None]}
        return record

    @property
    def reported_mode(self) -> str | None:
        """The permission mode the agent last reported; ``None`` when it reports none."""
        from garuda.runtime.roles import reported_mode

        process = self._process
        if process is None:
            return None
        return reported_mode(getattr(process, "session_config_options", []),
                             getattr(process, "session_modes", None))

    @property
    def effective_mode(self) -> dict[str, Any] | None:
        """The mode this session was confirmed to run in, for the session record."""
        return dict(self._effective_mode) if self._effective_mode is not None else None

    def confirm_mode(self, required: str, *, permission: str,
                     confined: bool = False) -> dict[str, Any]:
        """Refuse unless the agent now reports a mode that needs no more than the
        ``required`` authority (:func:`~garuda.runtime.roles.check_mode`)."""
        from garuda.runtime.roles import check_mode

        self._effective_mode = check_mode(
            self._runtime_id, required, self.reported_mode,
            adapter_version=self.reported_adapter_version, permission=permission,
            set_by_garuda=self._mode_set, confined=confined,
            declared=self._declared_modes)
        self._mode_checked = True
        return self.effective_mode

    async def secure_mode(self, permission: str = "smart") -> dict[str, Any]:
        """Select and confirm the mode for a run allowed ``permission`` and no role.

        Run paths with a role set the role's options and call
        :meth:`confirm_mode` themselves; any other path (SDK conversations, a
        prompt nobody secured) gets this before its first prompt, so an agent
        whose own default is broader than the run never acts (#700).
        """
        from garuda.runtime.roles import RolePlan, acp_options, harness_mode, required_authority

        plan = RolePlan(role="", runtime_id=self._runtime_id, kind="acp", permissions=permission)
        if harness_mode(plan) is not None:
            version = self.reported_adapter_version
            await self.set_config_options(acp_options(plan, version), adapter_version=version)
        return self.confirm_mode(required_authority(plan), permission=permission)

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

    @property
    def last_turn(self) -> dict[str, Any] | None:
        """``{"turn", "stop_reason", "approval_denied"}`` of the latest turn, or None
        before the first. ``stop_reason`` is the harness's raw v1 stop reason
        (``end_turn``, ``max_tokens``, ``max_turn_requests``, ``refusal``,
        ``cancelled``); it does not change how the turn settles (#824)."""
        return dict(self._last_turn) if self._last_turn is not None else None

    @property
    def last_denied(self) -> tuple[str, ...]:
        """The screened descriptions of the actions Garuda denied in the latest turn
        (agent-supplied titles: data, never instructions), oldest first."""
        return tuple(dict.fromkeys(self._denied))

    def _latest_rate_limit(self) -> dict[str, Any] | None:
        """The newest account limit snapshot the harness reported this session."""
        for event in reversed(self._events):
            usage = event.payload.get("usage") if event.kind is RuntimeEventKind.MESSAGE else None
            if isinstance(usage, dict) and isinstance(usage.get("rate_limit"), dict):
                return usage["rate_limit"]
        return None

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
        self._persist(self._events[-1])

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
        """Append selected normalized metadata, never ACP transcript content."""
        if self._persist_dir is None:
            return
        try:
            from garuda.acp.normalize import evidence_payload

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
                            **({"payload": selected}
                               if (selected := evidence_payload(event, self._trace_detail))
                               else {}),
                        },
                        default=str,
                    )
                    + "\n"
                )
        except OSError:
            # Observability must not turn a running external agent into a failure.
            return

    def _record_failure(
        self,
        exc: BaseException,
        *,
        phase: str,
        kind: str = "execution_failure",
        code: str | None = None,
        boundary: str | None = None,
    ) -> None:
        """Best-effort causal evidence; it can never replace ``exc`` or outcome."""
        if self._persist_dir is None:
            return
        try:
            from garuda.core.failure_evidence import append_causal_record, causal_record

            append_causal_record(
                self._persist_dir,
                causal_record(
                    exc,
                    phase=phase,
                    runtime_id=self._runtime_id,
                    adapter_version=self._agent_info.get("version"),
                    reported_agent=self._agent_info.get("name"),
                    protocol_version=self.version,
                    kind=kind,
                    code=code,
                    boundary=boundary,
                ),
            )
        except Exception:
            logger.debug("Could not retain ACP causal evidence", exc_info=True)

    async def _settle_failed_start(self, process: AcpProcess) -> Exception | None:
        """Close startup resources while retaining any cleanup uncertainty."""
        cleanup_error: Exception | None = None
        try:
            await process.close()
        except Exception as exc:
            # Preserve the actual process handle and prepared ownership so a
            # later close/recovery can prove cleanup rather than assume it.
            self._process = process
            cleanup_error = exc
            self._record_failure(
                exc,
                phase="cleanup",
                kind="cleanup_uncertainty",
                code="cleanup.uncertain",
                boundary="agent-process",
            )
        try:
            await self._close_consult()
        except Exception as exc:
            if cleanup_error is None:
                cleanup_error = exc
            self._record_failure(
                exc,
                phase="cleanup",
                kind="cleanup_uncertainty",
                code="cleanup.uncertain",
                boundary="consult-host",
            )
        if self._process is not process:
            self._retire_prepared_child()
        return cleanup_error

    def _new_process(self) -> AcpProcess:
        return AcpProcess(self._argv, extra_env=self._extra_env,
                          handshake_timeout=self._handshake_timeout,
                          handshake_hint=self._handshake_hint)

    async def start(self, *, task: str, session_id: str | None = None) -> RuntimeInfo:
        if self._state is not LifecycleState.DISCOVERED:
            raise RuntimeStartError(f"already started (state={self._state.value})")
        if not task:
            raise RuntimeStartError("task must not be empty")
        self._move(LifecycleState.STARTING)
        self._garuda_session_id = session_id or str(uuid.uuid4())
        self._record_trace_detail()
        process = self._new_process()
        try:
            with self._timed("startup"):
                await process.launch()
                self._record_prepared_launch(process)
                handshake = await process.initialize()
                self._refresh_prepared_identity()
            with self._timed("negotiation"):
                self._authority = negotiate(
                    self._policy,
                    AgentCapabilities.from_dict(handshake.get("agentCapabilities")),
                )
            quota = handshake.get("quota")
            self._quota = dict(quota) if isinstance(quota, dict) else None
            servers = await self._consult_servers(handshake, process)
            self._note_session_request("session/new", self._cwd or os.getcwd(), servers)
            self._agent_session_id = await process.session_new(
                cwd=self._cwd, mcp_servers=servers or None)
            if self._store is None and self._prepared_recorder is None:
                # No store means no persisted child record: a Garuda crash
                # would leave this child unrecoverable by `recover()`.
                logger.warning(
                    "ACP runtime %s started without a session store; its child "
                    "process is not recorded for restart recovery",
                    self._runtime_id,
                )
            elif self._store is not None:
                self._persist_identity(process)
        except AcpProtocolError as exc:
            self._note_error(exc)
            self._record_failure(exc, phase="startup")
            cleanup_error = await self._settle_failed_start(process)
            self._move(LifecycleState.FAILED)
            if cleanup_error is not None:
                raise cleanup_error from exc
            if "version mismatch" in str(exc) and self._setup_hint:
                raise AcpProtocolError(f"{exc} Upgrade the adapter: {self._setup_hint}") from exc
            raise
        except Exception as exc:
            self._note_error(exc)
            self._record_failure(exc, phase="startup")
            cleanup_error = await self._settle_failed_start(process)
            self._move(LifecycleState.FAILED)
            if cleanup_error is not None:
                raise cleanup_error from exc
            raise
        except asyncio.CancelledError as cancelled:
            # A cancelled start must not leave a launched child running with
            # no runtime that owns it: reap it, retire it, then propagate.
            cleanup_error = await self._settle_failed_start(process)
            self._move(LifecycleState.FAILED)
            if cleanup_error is not None:
                raise cleanup_error from cancelled
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
        # Promotes the `prepared` record written at launch, when there is one.
        record_child(
            self._store,
            self._garuda_session_id,
            runtime_id=self._runtime_id,
            pid=process.pid,
            process_group=process.pid,
        )
        self._recorded_child_pid = process.pid
        if self._prepared_child_pid == process.pid:
            self._prepared_child_pid = None
        from garuda.observability.agent_binding import active_runtime_tenure

        self._agent_segment["runtime_tenure"] = active_runtime_tenure(
            self._store, self._garuda_session_id, runtime_id=self.runtime_id,
            kind=self.kind.value, native_session_id=self.native_session_id,
        )

    def record_launch_with(self, recorder) -> None:
        """Record this handoff target's child as soon as it is launched.

        `recorder` is a `garuda.runtime.recovery.PreparedChildRecorder`: it
        writes a `prepared` child record the moment the process exists, so a
        Garuda crash before `bind_session` still leaves a record `recover()`
        can reap. Only a store-less runtime that has not started accepts one.
        """
        if self._state is not LifecycleState.DISCOVERED:
            raise RuntimeStartError("a launch recorder must be set before start")
        if self._store is not None:
            raise RuntimeStartError("a store-backed runtime records its child itself")
        self._prepared_recorder = recorder

    def _record_prepared_launch(self, process: AcpProcess) -> None:
        if self._prepared_recorder is None or self._store is not None:
            return
        if process.pid is None:
            raise RuntimeStartError("ACP process launched without a pid")
        # A failure raises into `start`, which closes the process: a target
        # that cannot be recorded does not run unrecorded.
        self._prepared_recorder.launched(process.pid)
        self._prepared_child_pid = process.pid

    def _refresh_prepared_identity(self) -> None:
        if self._prepared_child_pid is None or self._prepared_recorder is None:
            return
        # The handshake proved the agent itself is running; a launcher's
        # exec may have changed the identity captured at launch.
        self._prepared_recorder.running(self._prepared_child_pid)

    def _retire_prepared_child(self) -> None:
        """Retire a launch-recorded child that was reaped before binding."""
        pid, self._prepared_child_pid = self._prepared_child_pid, None
        if pid is None or self._prepared_recorder is None:
            return
        try:
            self._prepared_recorder.exited(pid)
        except Exception:
            # Left `prepared`, the record is still safe: recovery re-checks
            # the persisted identity before any signal.
            logger.warning("Could not retire prepared ACP child %s", pid, exc_info=True)

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
        self._record_trace_detail()

    @property
    def trace_detail(self) -> str | None:
        """The study-trace level this runtime persists its events at, or ``None``
        when it persists none (#691)."""
        return self._trace_detail if self._persist_dir is not None else None

    def _record_trace_detail(self) -> None:
        """Record in the session which trace level its persisted events use (#691),
        so a study knows what detail it can rely on. Only a store-backed, persisting
        runtime of an existing session records it; never fails the run."""
        if self._persist_dir is None or self._store is None or not self._garuda_session_id:
            return
        try:
            if not (Path(self._store.session_dir(self._garuda_session_id)) / "meta.json").is_file():
                return
            self._store.update_meta(self._garuda_session_id, {"trace_detail": self._trace_detail})
        except Exception:
            logger.warning("ACP trace level not recorded", exc_info=True)

    async def resume(self, *, native_session_id: str, session_id: str | None = None) -> RuntimeInfo:
        """Reattach to the live child, or — in a new process — reload the
        agent's own session with `session/load`.

        Cross-process load is attempted only when the caller has proved it
        for this adapter (`runtime.resume.PROVEN_LOAD`); an agent that no
        longer declares `loadSession` at the handshake raises
        :class:`ResumeUnavailable` so the caller can fall back to a brief.
        """
        if self._state is not LifecycleState.DISCOVERED:
            raise RuntimeStartError(f"cannot resume (state={self._state.value})")
        if self._process is not None:
            if self._agent_session_id != native_session_id:
                raise RuntimeStartError("the live child holds a different agent session")
            self._move(LifecycleState.STARTING)
            self._move(LifecycleState.IDLE)
            self._emit(RuntimeEventKind.LIFECYCLE, {"state": "resumed"})
            return self._info()
        self._move(LifecycleState.STARTING)
        self._garuda_session_id = session_id or str(uuid.uuid4())
        self._record_trace_detail()
        process = self._new_process()
        try:
            with self._timed("startup"):
                await process.launch()
                self._record_prepared_launch(process)
                handshake = await process.initialize()
                self._refresh_prepared_identity()
            caps = handshake.get("agentCapabilities")
            if not (isinstance(caps, dict) and caps.get("loadSession") is True):
                raise ResumeUnavailable("the agent no longer declares loadSession")
            with self._timed("negotiation"):
                self._authority = negotiate(self._policy, AgentCapabilities.from_dict(caps))
            servers = await self._consult_servers(handshake, process)
            self._note_session_request("session/load", self._cwd or os.getcwd(), servers)
            await process.session_load(native_session_id, self._cwd or os.getcwd(),
                                       servers or None)
            self._agent_session_id = native_session_id
            if self._store is not None:
                self._persist_identity(process)
        except BaseException as exc:
            if isinstance(exc, Exception):
                self._note_error(exc)
                self._record_failure(exc, phase="resume")
            cleanup_error = await self._settle_failed_start(process)
            self._move(LifecycleState.FAILED)
            if cleanup_error is not None:
                raise cleanup_error from exc
            raise
        self._process = process
        self._normalizer = AcpNormalizer(self._agent_session_id)
        self._move(LifecycleState.IDLE)
        self._emit(RuntimeEventKind.LIFECYCLE, {"state": "resumed"})
        return self._info()

    async def set_config_options(self, options: dict[str, str], *,
                                 adapter_version: str | None = None) -> dict[str, str]:
        """Apply exact session options before the first prompt (C.3).

        Each value must be one the agent offered in ``session/new``, and the
        agent's reply must show it selected; otherwise
        :class:`~garuda.runtime.roles.RoleRefused` and nothing is prompted.
        ``adapter_version`` is the version the options were proven for; the
        agent's ``initialize`` identity must report the same one.
        """
        from garuda.runtime.roles import PROVEN_MODE_OPTION, RoleRefused, check_offered, not_applied

        if not options:
            return {}
        if self._process is None or self._state is not LifecycleState.IDLE or self._turn:
            raise RuntimeNotActiveError("options are set once, before the first prompt")
        reported = self._agent_info.get("version")
        if adapter_version is not None and reported != adapter_version:
            raise RoleRefused("role.adapter_mismatch",
                              f"the agent reports version {reported!r} at initialize, but "
                              f"{adapter_version!r} was checked before launch")
        model = options.get("model")
        applied = {}
        # The model first: the other options the agent offers depend on it (#694),
        # so each one is checked against what the agent reports after the last set.
        for config_id, value in sorted(options.items(), key=lambda item: item[0] != "model"):
            check_offered({config_id: value},
                          getattr(self._process, "session_config_options", []),
                          model=model, runtime_id=self._runtime_id)
            current = await self._process.session_set_config_option(
                self._agent_session_id or "", config_id, value
            )
            now = next((o.get("currentValue") for o in current
                        if isinstance(o, dict) and o.get("id") == config_id), None)
            if now != value:
                raise not_applied(self._runtime_id, config_id, value, now, model=model)
            applied[config_id] = value
            if config_id == PROVEN_MODE_OPTION:
                self._mode_set = True
        if PROVEN_MODE_OPTION in applied:
            # A later set (or the agent itself) must not have moved the mode again.
            now = self.reported_mode
            if now != applied[PROVEN_MODE_OPTION]:
                raise not_applied(self._runtime_id, PROVEN_MODE_OPTION,
                                  applied[PROVEN_MODE_OPTION], now, model=model)
        return applied

    def _record_outbound_prompt(self, text: str) -> None:
        """Measure the attempted ACP request text, not the agent's system prompt."""
        import hashlib

        self._emit(RuntimeEventKind.OUTBOUND_PROMPT, {
            "digest": hashlib.sha256(text.encode("utf-8")).hexdigest(), "chars": len(text),
            "kind": "request_text", "status": "attempted",
            "agent_segment": {**self._agent_segment,
                              "native_session_id": self._agent_session_id},
        })

    async def prompt(self, text: str, *, timeout: float | None = None) -> int:
        if self._process is None or self._normalizer is None:
            raise RuntimeStartError("runtime is not started")
        if self._state in (LifecycleState.CLOSED, LifecycleState.FAILED):
            raise RuntimeClosedError(f"runtime is {self._state.value}")
        if self._state is not LifecycleState.IDLE:
            raise RuntimeNotActiveError(f"prompt needs IDLE, state={self._state.value}")
        if not text:
            raise RuntimeNotActiveError("prompt text must not be empty")
        if not self._mode_checked:
            # Nothing secured this session's mode: a run with no role, allowed
            # at most `smart` unless its owner said otherwise, so an agent
            # default broader than that refuses.
            await self.secure_mode(self._permission)
        # Open the normalizer turn before RUNNING: if it refuses, the runtime
        # is not left mid-turn with nothing driving it.
        self._normalizer.new_turn()
        self._move(LifecycleState.RUNNING)
        self._turn += 1
        self._denied = []
        self._last_turn = {"turn": self._turn, "stop_reason": None, "approval_denied": False}
        self._plan_result = None
        self._cancel_requested = False
        self._calls_in_flight = set()
        self._record_outbound_prompt(text)
        prompt_task = asyncio.ensure_future(
            self._process.session_prompt(self._agent_session_id or "", text)
        )
        try:
            with self._timed("turn"):
                if timeout is not None:
                    result = await asyncio.wait_for(self._drain_until_done(prompt_task), timeout)
                else:
                    result = await self._drain_until_done(prompt_task)
        except asyncio.CancelledError:
            # The awaiting task was cancelled mid-turn (#803): end the turn as
            # an explicit cancel() would, so the harness stops generating and
            # the runtime is CLOSED, never left RUNNING with a live child.
            # `_notify_cancel` bounds the wait on an agent that never reads it.
            await self._cancel_pending_approvals()
            await self._notify_cancel()
            prompt_task.cancel()
            await asyncio.gather(prompt_task, return_exceptions=True)
            self._finish_turn("cancelled")
            try:
                await self._close_process()
            finally:
                self._move(LifecycleState.CANCELLING)
                self._emit(RuntimeEventKind.LIFECYCLE, {"state": "cancelled", "reason": "task cancelled"})
                self._move(LifecycleState.CLOSED)
            raise
        except TimeoutError as exc:
            prompt_task.cancel()
            timeout_error = AcpTimeoutError("prompt exceeded its deadline")
            self._note_error(timeout_error)
            self._record_failure(timeout_error, phase="prompt")
            await self._notify_cancel()
            await self._close_process()
            self._emit(RuntimeEventKind.LIFECYCLE, {"state": "failed", "reason": "timeout"})
            self._move(LifecycleState.FAILED)
            raise timeout_error from exc
        except AcpSilenceError as silence_error:
            # The opt-in watchdog (#828): end the turn as the deadline path does.
            prompt_task.cancel()
            self._note_error(silence_error)
            self._record_failure(silence_error, phase="prompt")
            await self._notify_cancel()
            await self._close_process()
            self._emit(RuntimeEventKind.LIFECYCLE, {"state": "failed", "reason": "silence"})
            self._move(LifecycleState.FAILED)
            raise
        except AcpCancelledError:
            self._finish_turn("cancelled")
            await self._close_process()
            self._move(LifecycleState.CANCELLING)
            self._emit(RuntimeEventKind.LIFECYCLE, {"state": "cancelled"})
            self._move(LifecycleState.CLOSED)
            raise
        except AcpError as exc:
            from garuda.core.failure_evidence import classify_exception

            self._last_turn["approval_denied"] = bool(self._denied)
            if isinstance(exc, AcpUsageLimitError):
                # The reset time the harness reported before it refused (#824).
                exc.note_rate_limit(self._latest_rate_limit())
            self._note_error(exc)
            self._record_failure(exc, phase="prompt")
            self._finish_turn("failed", detail=str(exc), code=classify_exception(exc)[0])
            await self._close_process()
            self._move(LifecycleState.FAILED)
            raise
        stop = result.get("stopReason") if isinstance(result, dict) else None
        plan = self._plan_result if stop == "cancelled" and not self._cancel_requested else None
        denied_any = bool(self._denied)
        self._last_turn["approval_denied"] = denied_any
        try:
            if not isinstance(stop, str):
                raise AcpProtocolError("session/prompt returned no valid stopReason")
            raw_stop = stop
            if plan is not None:
                # The agent stopped because Garuda refused its plan-mode exit
                # (#771): the plan is the reply and the turn is complete; the
                # harness stays in plan mode for any later turn.
                self._absorb(self._normalizer.feed({"sessionUpdate": "agent_message_chunk",
                                                    "content": {"type": "text", "text": plan}}))
                self._emit(RuntimeEventKind.LIFECYCLE, {
                    "state": "plan_produced", "chars": len(plan), "mode": PLAN_MODE,
                    "detail": "plan returned as the result; leaving plan mode refused"})
                stop = "end_turn"
            closing = self._normalizer.finish(stop, raw_stop_reason=raw_stop,
                                              approval_denied=denied_any)
            self._last_turn["stop_reason"] = raw_stop
        except AcpProtocolError as exc:
            # An unknown stop reason or an orphaned tool_call_update means the
            # trail cannot be closed truthfully: fail and reap, never stay
            # RUNNING with a live process.
            self._emit(RuntimeEventKind.ERROR, {"message": str(exc)})
            self._record_failure(exc, phase="prompt-normalization")
            await self._close_process()
            self._move(LifecycleState.FAILED)
            raise
        self._absorb(closing)
        if stop == "cancelled":
            # The agent ended the turn cancelled (e.g. its permission request
            # was answered `cancelled`). The normalizer has closed the
            # session, so the runtime closes too instead of idling on a trail
            # that can take no further turn.
            denied = ", ".join(dict.fromkeys(self._denied))
            if denied:
                self._record_failure(
                    PermissionError("Garuda denied a requested action"),
                    phase="approval",
                    code="permission.denied",
                    boundary="permission-broker",
                )
            await self._close_process()
            self._move(LifecycleState.CANCELLING)
            self._move(LifecycleState.CLOSED)
            raise AcpCancelledError("agent ended the turn cancelled" + (
                f" after Garuda denied: {denied}" if denied else ""))
        self._move(LifecycleState.IDLE)
        return self._turn

    def _finish_turn(self, reason: str, *, detail: str = "", code: str = "") -> None:
        """Close the normalizer turn on a path that must still reap.

        A trail that cannot close cleanly (updates for unknown tool calls) is
        recorded as an error event rather than raised past the cleanup.
        """
        assert self._normalizer is not None
        try:
            self._absorb(self._normalizer.finish(reason, detail=detail, code=code))
        except AcpProtocolError as exc:
            self._emit(RuntimeEventKind.ERROR, {"message": str(exc)})

    async def _drain_until_done(self, prompt_task: asyncio.Task) -> Any:
        """Feed notifications as they stream, returning the prompt result."""
        assert self._process is not None and self._normalizer is not None
        heard = time.monotonic()
        while True:
            done, _ = await asyncio.wait({prompt_task}, timeout=0.05)
            notifications = self._process.drain_notifications()
            for notification in notifications:
                if notification.get("method") != "session/update":
                    continue
                update = (notification.get("params") or {}).get("update")
                if update:
                    if self._silence is not None and isinstance(update, dict):
                        self._track_tool_call(update)
                    self._absorb(self._normalizer.feed(update))
            requests = self._process.drain_requests()
            for request in requests:
                await self._on_agent_request(request)
            if done:
                return prompt_task.result()
            if self._silence is not None:
                now = time.monotonic()
                limit = self._silence_limit()
                if notifications or requests or limit is None:
                    heard = now  # activity, or a parked approval: the clock restarts
                elif now - heard >= limit:
                    raise AcpSilenceError(
                        f"the agent sent nothing for {limit:g}s"
                        + (" while a tool call was running" if self._calls_in_flight else "")
                        + "; the turn was cancelled and the agent stopped")

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
        action = _approval_action(tool_call, options, approval_id)
        self._pending_approvals[approval_id] = (request_id, options, action)
        if self._plan_mode_exit(tool_call):
            await self._refuse_plan_exit(approval_id, action, tool_call, options)
            return
        if self._consult_host is not None and self._consult_host.allows_permission(
                params, self._agent_info, agent_session_id=self._agent_session_id):
            try:
                await self.permission_response(approval_id=approval_id, allow=True)
            except (RuntimeProtocolError, RuntimeStartError, AcpError):
                return  # already answered, cancelled, or the process is gone
            self._emit(RuntimeEventKind.LIFECYCLE, {"state": "consult_tool_allowed"})
            return
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
        if self._confined_approval_handler is not None:
            tool_kind = normalize_tool_kind(tool_call.get("kind"))
            allowed, reason = self._confined_approval_handler(
                action, tool_kind, _pick_option(options, allow=True) is not None,
            )
            self._emit(RuntimeEventKind.LIFECYCLE, {
                "state": "approval_settled", "approval_id": approval_id,
                "tool_kind": tool_kind, "outcome": "allow" if allowed else "deny", "reason": reason,
            })
            await self.permission_response(approval_id=approval_id, allow=allowed)
        elif self._approval_handler is not None:
            task = asyncio.ensure_future(self._answer_with_handler(approval_id, action))
            self._answer_tasks.add(task)
            task.add_done_callback(self._answer_tasks.discard)

    def _plan_mode_exit(self, tool_call: dict[str, Any]) -> bool:
        """Whether this request asks to leave the harness's plan mode (#771).

        Any ``switch_mode`` request while the agent reports ``plan`` counts,
        with or without a plan: a plan-mode run must never switch into a mode
        that edits, so no handler gets the chance to allow it.
        """
        return (normalize_tool_kind(tool_call.get("kind")) == "switch_mode"
                and self.reported_mode == PLAN_MODE)

    async def _refuse_plan_exit(self, approval_id: str, action: str,
                                tool_call: dict[str, Any], options: list) -> None:
        """Refuse a plan-mode exit and keep its plan as the turn's reply.

        The reject option is the answer claude-agent-acp reads as "keep
        planning": it stops the turn (``cancelled``) and leaves the session in
        plan mode. Without a reject option the answer is ``cancelled``, which
        refuses too. Never an allow option: each one switches mode.
        """
        self._emit(RuntimeEventKind.APPROVAL_REQUEST, {
            "approval_id": approval_id, "action": action,
            "tool_call_id": tool_call.get("toolCallId", ""),
            "options": [{"option_id": option.get("optionId", ""), "kind": option.get("kind", ""),
                         "name": option.get("name", "")} for option in options],
        })
        self._emit(RuntimeEventKind.LIFECYCLE, {
            "state": "approval_settled", "approval_id": approval_id,
            "tool_kind": "switch_mode", "outcome": "deny",
            "reason": "plan mode: the plan is the result; Garuda does not leave plan mode",
        })
        await self.permission_response(approval_id=approval_id, allow=False)
        # Not a denial that fails the turn: the refusal is how planning ends.
        if self._denied and self._denied[-1] == action:
            self._denied.pop()
        self._plan_result = _plan_text(tool_call)

    async def _answer_with_handler(self, approval_id: str, action: str) -> None:
        assert self._approval_handler is not None
        decision = None
        try:
            decision = await self._approval_handler(action)
            allowed = bool(decision)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("ACP approval handler failed for %s; denying", approval_id, exc_info=True)
            allowed = False
        try:
            await self.permission_response(approval_id=approval_id, allow=allowed)
        except (RuntimeProtocolError, RuntimeStartError, AcpError):
            # Already answered, cancelled, or the process is gone.
            return
        # The runtime has the answer: a broker decision records its delivery (B.8).
        acknowledge = getattr(decision, "acknowledge", None)
        if acknowledge is not None:
            try:
                acknowledge()
            except Exception:
                logger.warning("Recording delivery of %s failed", approval_id, exc_info=True)

    async def _cancel_pending_approvals(self) -> None:
        """v1 requires every open permission request to be answered
        `cancelled` when its turn is cancelled."""
        for task in list(self._answer_tasks):
            task.cancel()
        pending, self._pending_approvals = self._pending_approvals, {}
        if self._process is None:
            return
        for request_id, _options, _action in pending.values():
            try:
                await self._process.respond(
                    request_id, result={"outcome": {"outcome": "cancelled"}}
                )
            except AcpError:
                pass

    async def _notify_cancel(self) -> None:
        """Best effort `session/cancel` for a turn Garuda abandons (#701).

        The process is reaped next whatever happens here, so a stuck or
        closed transport is logged, never raised.
        """
        if self._process is None or not self._process.is_running:
            return
        try:
            await asyncio.wait_for(self._process.session_cancel(self._agent_session_id or ""), 2)
        except Exception:
            logger.debug("session/cancel for a timed-out turn was not delivered", exc_info=True)

    async def _close_process(self) -> None:
        # No interactive answerer outlives the process it would answer.
        for task in list(self._answer_tasks):
            task.cancel()
        try:
            if self._process is not None:
                await self._process.close()
            if self._consult_host is not None:
                await self._consult_host.close()
            self._mark_child_exited()
        except Exception as exc:
            self._record_failure(
                exc,
                phase="cleanup",
                kind="cleanup_uncertainty",
                code="cleanup.uncertain",
                boundary="agent-process",
            )
            raise

    def _mark_child_exited(self) -> None:
        """Retire the recorded child once `AcpProcess.close` has reaped it.

        A record left `live` after a clean exit makes a later recovery probe
        whatever process reuses that PID. A crash between `close` and this
        write still leaves it `live`; recovery's persisted start-time/command
        identity check is what keeps that record from signalling a stranger.
        """
        self._retire_prepared_child()
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
        request_id, options, action = entry
        option_id = _pick_option(options, allow)
        outcome = (
            {"outcome": "selected", "optionId": option_id}
            if option_id is not None
            else {"outcome": "cancelled"}
        )
        with self._timed("approval"):
            await self._process.respond(request_id, result={"outcome": outcome})
        if not allow or option_id is None:
            self._denied.append(action)
            # A later denial, not a plan exit before it, is what ends this turn.
            self._plan_result = None

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
        self._cancel_requested = True
        await self._cancel_pending_approvals()
        # The in-flight prompt can observe its ACP error and finish verified
        # process cleanup while cancellation is crossing lease.race(). In that
        # terminal race there is no peer left to notify; close() below remains
        # responsible for the idempotent group-cleanup receipt. Do not turn the
        # caller's cancellation into a protocol failure merely because the
        # already-closed transport cannot accept session/cancel.
        if self._process is not None and self._process.is_running:
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
        if self._state is LifecycleState.DISCOVERED:
            # Never started (a refusal before `start`): there is no session to
            # record a lifecycle event against, only a child to make sure of.
            await self._close_process()
            return
        if self._state in (LifecycleState.CLOSED, LifecycleState.FAILED):
            await self._close_process()
            self._persist_metrics()
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


#: The harness mode a plan-mode run keeps (claude-agent-acp's ``plan``).
PLAN_MODE = "plan"
#: The longest plan kept as a turn's reply.
_PLAN_LIMIT = 200_000


def _plan_text(tool_call: dict[str, Any]) -> str | None:
    """The plan a plan-mode exit request carries: its ``rawInput.plan``
    (claude-agent-acp's ExitPlanMode input), else the text of its content."""
    raw = tool_call.get("rawInput")
    plan = raw.get("plan") if isinstance(raw, dict) else None
    if not isinstance(plan, str) or not plan.strip():
        parts = []
        for item in tool_call.get("content") or []:
            block = item.get("content") if isinstance(item, dict) else None
            if isinstance(block, dict) and block.get("type") == "text" and isinstance(
                    block.get("text"), str):
                parts.append(block["text"])
        plan = "\n\n".join(parts)
    plan = plan.strip()
    return plan[:_PLAN_LIMIT] or None


def _pick_option(options: list[dict[str, Any]], allow: bool) -> str | None:
    """The narrowest option matching the decision, or None (answer cancelled)."""
    kinds = ("allow_once",) if allow else ("reject_once", "reject_always")
    for kind in kinds:
        for option in options:
            option_id = option.get("optionId")
            if option.get("kind") == kind and isinstance(option_id, str) and option_id:
                return option_id
    return None


def _approval_action(tool_call: dict, options: list, approval_id: str) -> str:
    """The approval's screened description, bound to the whole request.

    The title alone let two different requests share one description, and so
    one approval digest. The full tool call (input, locations, kind) and the
    offered options enter only as a digest, so nothing raw is displayed or
    persisted, and the adapter's own request id links the broker record back
    to the ACP request it answers.
    """
    import hashlib
    import json

    title = str(tool_call.get("title") or tool_call.get("toolCallId") or "permission")[:300]
    payload = json.dumps({"tool_call": tool_call, "options": options}, sort_keys=True,
                         separators=(",", ":"), default=str)
    request = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
    kind = str(tool_call.get("kind") or "unknown")[:40]
    call = str(tool_call.get("toolCallId") or "")[:80]
    return f"{title} [acp {approval_id}; kind={kind}; call={call}; request={request}]"

