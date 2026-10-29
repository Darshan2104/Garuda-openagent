"""ACP subprocess lifecycle over stdio (P0.12, issue #21).

`AcpProcess` launches one configured agent process and speaks ACP v1:
initialize, session/new, session/prompt, session/cancel, and bidirectional
JSON-RPC notifications/requests over newline-delimited JSON. Agent-initiated
requests (`session/request_permission`, and any client method Garuda did not
advertise) are queued for the adapter, which answers each exactly once. stderr is drained
separately into a bounded diagnostics ring that never touches the protocol.

Safety posture: the argv comes from the caller (the registry-resolved
allowlist, wired by a later adapter issue) — this module never searches PATH
beyond what execvp already does and never inherits the parent environment.
The child gets a minimal explicit environment plus caller-supplied extras, so
no secret crosses the boundary by accident. Processes launch in their own
session (`start_new_session`) and close() always reaps the processes that stay
in that group: SIGTERM, a grace period, SIGKILL, then bounded verification.
This is a cleanup guardrail, not confinement; a descendant that starts a new
session or otherwise leaves the group is outside this module's ownership proof.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import subprocess
import time
from collections import deque
from typing import Any

from garuda.acp.protocol import (
    ACP_VERSION,
    MAX_FRAME_BYTES,
    AcpCancelledError,
    AcpExitError,
    AcpProtocolError,
    AcpRemoteError,
    AcpTimeoutError,
    decode_frame,
    encode_frame,
)

logger = logging.getLogger(__name__)

HANDSHAKE_TIMEOUT = 30.0
CALL_TIMEOUT = 120.0
TERM_GRACE = 5.0
KILL_GRACE = 3.0
GROUP_POLL_INTERVAL = 0.05
PIPE_DRAIN_TIMEOUT = 2.0
STDERR_RING = 50
_MISSING = object()
_PS_CANDIDATES = ("/bin/ps", "/usr/bin/ps")


def child_environment(extra_env: dict[str, str] | None = None) -> dict[str, str]:
    """Minimal explicit environment shared by ACP and trusted ACP probes.

    Ambient vendor configuration such as ``CODEX_HOME`` is intentionally not
    inherited. A caller that needs one executable override supplies it in
    ``extra_env`` (for example ``CODEX_PATH``).
    """
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", ""),
        # The account name, not a credential: Claude Code on macOS keys its
        # Keychain login by it and reports logged out without it.
        "USER": os.environ.get("USER", ""),
        "LANG": "C.UTF-8",
    }
    env.update(extra_env or {})
    return {key: value for key, value in env.items() if value}


def _live_group_members(process_group: int) -> tuple[int, ...]:
    """Return live, non-zombie members of one process group.

    An unavailable or malformed process-table answer is uncertainty, not
    evidence of closure. ``ps`` is used because its pid/pgid/stat columns are
    available on both Linux and macOS, where Garuda runs.
    """
    ps = next((path for path in _PS_CANDIDATES if os.path.exists(path)), None)
    if ps is None:
        raise AcpProtocolError("cannot verify ACP process-group cleanup: ps is unavailable")
    try:
        result = subprocess.run(
            [ps, "-Ao", "pid=,pgid=,stat="],
            capture_output=True,
            text=True,
            timeout=2.0,
            env={"LC_ALL": "C", "PATH": "/usr/bin:/bin"},
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise AcpProtocolError(
            f"cannot verify ACP process-group cleanup: ps failed: {exc}"
        ) from exc
    if result.returncode != 0:
        detail = result.stderr.strip()[-300:]
        raise AcpProtocolError(
            "cannot verify ACP process-group cleanup: "
            f"ps exited {result.returncode}{': ' + detail if detail else ''}"
        )
    members: list[int] = []
    for line in result.stdout.splitlines():
        fields = line.split(None, 2)
        if len(fields) != 3:
            if line.strip():
                raise AcpProtocolError("cannot parse process table during ACP cleanup")
            continue
        try:
            pid, pgid = int(fields[0]), int(fields[1])
        except ValueError as exc:
            raise AcpProtocolError("cannot parse process table during ACP cleanup") from exc
        if pgid == process_group and not fields[2].startswith("Z"):
            members.append(pid)
    return tuple(members)


class AcpProcess:
    """One managed ACP agent subprocess."""

    def __init__(
        self,
        argv: list[str],
        *,
        extra_env: dict[str, str] | None = None,
        call_timeout: float = CALL_TIMEOUT,
        cwd: str | None = None,
    ):
        if not argv or any(not isinstance(p, str) or not p for p in argv):
            raise AcpProtocolError("argv must be a non-empty list of strings")
        self._argv = list(argv)
        self._extra_env = dict(extra_env or {})
        self._call_timeout = call_timeout
        self._cwd = cwd
        self._process: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task | None = None
        self._stderr_task: asyncio.Task | None = None
        self._buffer = b""
        self._next_id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._notifications: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._requests: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._stderr_ring: deque[str] = deque(maxlen=STDERR_RING)
        self._closed = False
        self._close_task: asyncio.Task[None] | None = None
        self._process_group: int | None = None
        self._leader_identity: str | None = None

    @property
    def pid(self) -> int | None:
        return self._process.pid if self._process else None

    @property
    def stderr_tail(self) -> str:
        return "\n".join(self._stderr_ring)

    def _child_env(self) -> dict[str, str]:
        return child_environment(self._extra_env)

    async def launch(self) -> None:
        """Spawn the process and start the reader + stderr-drain tasks."""
        if self._closed:
            raise AcpProtocolError("process is closed; launch after close is refused")
        if self._process is not None:
            raise AcpProtocolError("already launched")
        self._process = await asyncio.create_subprocess_exec(
            *self._argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self._child_env(),
            cwd=self._cwd,
            start_new_session=True,
        )
        try:
            self._bind_process_group()
        except Exception:
            try:
                await self._abort_unbound_launch(self._process)
            except Exception as cleanup_error:
                raise AcpProtocolError(
                    "ACP launch identity failed and process cleanup was unproved"
                ) from cleanup_error
            raise
        self._reader_task = asyncio.ensure_future(self._read_loop())
        self._stderr_task = asyncio.ensure_future(self._drain_stderr())

    def _bind_process_group(self) -> None:
        """Retain the isolated group's PID-reuse-resistant launch identity."""
        assert self._process is not None
        pid = self._process.pid
        try:
            process_group = os.getpgid(pid)
        except OSError as exc:
            raise AcpProtocolError(
                f"cannot bind ACP process group for pid {pid}: {exc}"
            ) from exc
        if process_group != pid or process_group in {os.getpid(), os.getpgrp()}:
            raise AcpProtocolError(
                f"ACP child {pid} did not lead a private process group"
            )
        from garuda.runtime.recovery import _process_identity

        try:
            identity = _process_identity(pid)
        except Exception as exc:
            raise AcpProtocolError(
                f"cannot bind ACP process identity for pid {pid}: {exc}"
            ) from exc
        if not identity:
            raise AcpProtocolError(f"cannot bind ACP process identity for pid {pid}")
        self._process_group = process_group
        self._leader_identity = identity

    @staticmethod
    async def _abort_unbound_launch(process: asyncio.subprocess.Process) -> None:
        """Reap a just-spawned child when its durable group identity cannot bind."""
        try:
            if os.getpgid(process.pid) == process.pid:
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
        except ProcessLookupError:
            pass
        except OSError as exc:
            raise AcpProtocolError(
                f"could not stop unbound ACP child {process.pid}: {exc}"
            ) from exc
        try:
            await asyncio.wait_for(asyncio.shield(process.wait()), KILL_GRACE)
        except TimeoutError as exc:
            raise AcpProtocolError(
                f"unbound ACP child {process.pid} did not exit"
            ) from exc

    async def _read_loop(self) -> None:
        assert self._process is not None and self._process.stdout is not None
        try:
            while True:
                chunk = await self._process.stdout.read(65536)
                if not chunk:
                    # stdout EOF usually means the exit follows within
                    # milliseconds; reap first so the error carries the real
                    # code instead of a None the process hadn't published yet.
                    try:
                        await asyncio.wait_for(self._process.wait(), 5.0)
                    except TimeoutError:
                        self._on_transport_error(
                            AcpProtocolError("agent closed stdout without exiting")
                        )
                        return
                    await self._on_eof()
                    return
                self._buffer += chunk
                if b"\n" not in chunk:
                    # Still inside one line: skip re-scanning the whole buffer
                    # (quadratic for a large frame) and only enforce the bound.
                    if len(self._buffer) > MAX_FRAME_BYTES:
                        self._on_transport_error(
                            AcpProtocolError("reader buffer exceeds maximum frame size")
                        )
                        return
                    continue
                while True:
                    try:
                        message, self._buffer = decode_frame(self._buffer)
                    except ValueError:
                        break
                    except AcpProtocolError as exc:
                        self._on_transport_error(exc)
                        return
                    self._route(message)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # reader must never die silently
            logger.warning("ACP reader failed for %r", self._argv, exc_info=True)
            self._on_transport_error(AcpProtocolError(f"reader failed: {exc}"))

    async def _drain_stderr(self) -> None:
        assert self._process is not None and self._process.stderr is not None
        try:
            while True:
                line = await self._process.stderr.readline()
                if not line:
                    return
                self._stderr_ring.append(line.decode("utf-8", "replace").rstrip())
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("ACP stderr drain ended", exc_info=True)

    def _route(self, message: dict[str, Any]) -> None:
        if "id" in message and ("result" in message or "error" in message):
            # Python bool/float keys can alias our integer request IDs. Only
            # exact protocol ID types may reach the pending-request mapping.
            if type(message["id"]) not in (int, str):
                self._on_transport_error(AcpProtocolError("malformed JSON-RPC response id"))
                return
            response_error = None
            if "error" in message:
                error = message["error"]
                if ("result" in message or not isinstance(error, dict)
                        or type(error.get("code")) is not int
                        or not isinstance(error.get("message"), str)):
                    self._on_transport_error(AcpProtocolError("malformed JSON-RPC error response"))
                    return
                response_error = AcpRemoteError(
                    f"agent error {error['code']}: {error['message']}", remote_code=error["code"]
                )
            future = self._pending.pop(message["id"], None)
            if future is not None and not future.done():
                # Resolve only after the current read callback returns. An
                # ACP response can be followed by a notification in the same
                # stdout chunk; completing inline lets a waiter run before
                # that notification is routed, making drain_notifications()
                # timing-dependent across event-loop implementations.
                loop = asyncio.get_running_loop()
                if response_error is not None:
                    loop.call_soon(
                        self._settle_future,
                        future,
                        _MISSING,
                        response_error,
                    )
                else:
                    loop.call_soon(
                        self._settle_future, future, message.get("result"), None
                    )
        elif message.get("method"):
            if "id" in message:
                self._requests.put_nowait(message)
            else:
                self._notifications.put_nowait(message)
        else:
            self._on_transport_error(AcpProtocolError(f"unroutable message: {message!r}"))

    @staticmethod
    def _settle_future(
        future: asyncio.Future, result: Any = _MISSING, error: Exception | None = None
    ) -> None:
        if future.done():
            return
        if error is not None:
            future.set_exception(error)
        else:
            future.set_result(result)

    def _fail_all_pending(self, exc: Exception) -> None:
        pending, self._pending = self._pending, {}
        for future in pending.values():
            if not future.done():
                future.set_exception(exc)

    async def _wait_for_stderr(self) -> None:
        """Finish the existing drain after child exit before surfacing diagnostics."""
        if self._stderr_task is None:
            return
        try:
            await asyncio.shield(self._stderr_task)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("ACP stderr drain did not finish cleanly", exc_info=True)

    def _exit_error(self, message: str) -> AcpExitError:
        code = self._process.returncode if self._process else None
        return AcpExitError(
            f"{message} (code={code}); stderr: {self.stderr_tail[-500:]}",
            exit_code=code,
            stderr_tail=self.stderr_tail,
        )

    async def _on_eof(self) -> None:
        await self._wait_for_stderr()
        self._fail_all_pending(self._exit_error("agent exited"))

    def _on_transport_error(self, exc: Exception) -> None:
        self._fail_all_pending(exc)

    async def _call(
        self, method: str, params: dict[str, Any], *, timeout: Any = _MISSING
    ) -> Any:
        """One request. `timeout` omitted uses the call deadline; `None` waits
        without one (a prompt turn is bounded by its caller, not here)."""
        if self._process is None or self._process.stdin is None:
            raise AcpProtocolError("process is not running")
        if self._process.returncode is not None:
            await self._wait_for_stderr()
            raise self._exit_error("agent already exited")
        self._next_id += 1
        call_id = self._next_id
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        self._pending[call_id] = future
        try:
            self._process.stdin.write(
                encode_frame({"jsonrpc": "2.0", "id": call_id, "method": method, "params": params})
            )
            await self._process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as exc:
            self._pending.pop(call_id, None)
            raise AcpExitError(f"agent stdin broken: {exc}", exit_code=None) from exc
        try:
            deadline = self._call_timeout if timeout is _MISSING else timeout
            return await asyncio.wait_for(future, deadline)
        except TimeoutError as exc:
            self._pending.pop(call_id, None)
            raise AcpTimeoutError(f"{method} exceeded its deadline") from exc

    async def _notify(self, method: str, params: dict[str, Any]) -> None:
        """Write one notification and flush it.

        Awaiting the drain matters: `session/cancel` (and later approval
        answers) must reach the agent before the caller proceeds. A bare
        `stdin.write` can leave the bytes buffered in the transport while the
        local pending calls are already failed — a cancel the agent never saw.
        """
        await self._notify_payload(
            {"jsonrpc": "2.0", "method": method, "params": params}
        )

    async def _notify_payload(self, payload: dict[str, Any]) -> None:
        if self._process is None or self._process.stdin is None:
            raise AcpProtocolError("process is not running")
        try:
            self._process.stdin.write(encode_frame(payload))
            await self._process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as exc:
            raise AcpExitError(f"agent stdin broken: {exc}", exit_code=None) from exc

    async def initialize(self, *, timeout: float = HANDSHAKE_TIMEOUT) -> dict[str, Any]:
        """Handshake. A version mismatch fails here, never mid-session."""
        result = await self._call(
            "initialize",
            {
                "protocolVersion": ACP_VERSION,
                "clientCapabilities": {},
                "clientInfo": {"name": "garuda", "version": "0.1.0"},
            },
            timeout=timeout,
        )
        if not isinstance(result, dict):
            raise AcpProtocolError("initialize result must be an object")
        version = result.get("protocolVersion")
        if version != ACP_VERSION or isinstance(version, bool):
            raise AcpProtocolError(
                f"ACP version mismatch: expected {ACP_VERSION!r}, got {version!r}"
            )
        return result

    async def session_new(
        self,
        cwd: str | None = None,
        mcp_servers: list[dict[str, Any]] | None = None,
        *,
        timeout: float = HANDSHAKE_TIMEOUT,
    ) -> str:
        if cwd is None:
            cwd = os.getcwd()
        if not os.path.isabs(cwd):
            raise AcpProtocolError("session/new cwd must be absolute")
        if mcp_servers is not None and any(
            not isinstance(server, dict) for server in mcp_servers
        ):
            raise AcpProtocolError("session/new mcpServers must contain objects")
        result = await self._call(
            "session/new",
            {"cwd": cwd, "mcpServers": list(mcp_servers or [])},
            timeout=timeout,
        )
        session_id = result.get("sessionId") if isinstance(result, dict) else None
        if not session_id:
            raise AcpProtocolError("session/new returned no sessionId")
        options = result.get("configOptions") if isinstance(result, dict) else None
        self.session_config_options = list(options) if isinstance(options, list) else []
        return session_id

    async def session_set_config_option(
        self, session_id: str, config_id: str, value: str, *, timeout: float = HANDSHAKE_TIMEOUT
    ) -> list[dict[str, Any]]:
        """Set one session option; returns the agent's options after the change."""
        result = await self._call(
            "session/set_config_option",
            {"sessionId": session_id, "configId": config_id, "value": value},
            timeout=timeout,
        )
        options = result.get("configOptions") if isinstance(result, dict) else None
        return list(options) if isinstance(options, list) else []

    async def session_load(
        self,
        session_id: str,
        cwd: str,
        mcp_servers: list[dict[str, Any]] | None = None,
        *,
        timeout: float = HANDSHAKE_TIMEOUT,
    ) -> None:
        """Reload an agent session this agent persisted (`session/load`)."""
        if not os.path.isabs(cwd):
            raise AcpProtocolError("session/load cwd must be absolute")
        await self._call(
            "session/load",
            {"sessionId": session_id, "cwd": cwd, "mcpServers": list(mcp_servers or [])},
            timeout=timeout,
        )

    async def session_prompt(
        self,
        session_id: str,
        text: str | list[dict[str, Any]],
        *,
        timeout: float | None = None,
    ) -> Any:
        prompt = [{"type": "text", "text": text}] if isinstance(text, str) else text
        if not prompt or any(not isinstance(block, dict) for block in prompt):
            raise AcpProtocolError("session/prompt requires content blocks")
        return await self._call(
            "session/prompt",
            {"sessionId": session_id, "prompt": prompt},
            timeout=timeout,
        )

    async def session_cancel(self, session_id: str) -> None:
        """Cancel a prompt: notify the agent and fail local pending calls."""
        try:
            await self._notify("session/cancel", {"sessionId": session_id})
        finally:
            self._fail_all_pending(AcpCancelledError("cancelled by caller"))

    async def next_notification(self, timeout: float | None = None) -> dict[str, Any] | None:
        """Wait for the next agent notification. None on timeout, never raises."""
        try:
            return await asyncio.wait_for(self._notifications.get(), timeout)
        except TimeoutError:
            return None

    async def next_request(self) -> dict[str, Any]:
        """Wait for an agent request that requires a client response."""
        return await self._requests.get()

    async def respond(
        self,
        request_id: int | str | None,
        *,
        result: Any = _MISSING,
        error: dict[str, Any] | None = None,
    ) -> None:
        """Respond to an agent-initiated JSON-RPC request exactly once."""
        if (result is _MISSING) == (error is None):
            raise AcpProtocolError("response requires exactly one of result or error")
        payload: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id}
        if error is not None:
            payload["error"] = error
        else:
            payload["result"] = result
        await self._notify_payload(payload)

    def drain_requests(self) -> list[dict[str, Any]]:
        """Agent-initiated requests waiting for a response, oldest first."""
        out = []
        while not self._requests.empty():
            out.append(self._requests.get_nowait())
        return out

    def drain_notifications(self) -> list[dict[str, Any]]:
        out = []
        while not self._notifications.empty():
            out.append(self._notifications.get_nowait())
        return out

    @property
    def is_running(self) -> bool:
        return self._process is not None and self._process.returncode is None

    async def close(self) -> None:
        """Terminate, verify, and reap the owned process group.

        Repeated calls share one cleanup receipt. Caller cancellation is
        delayed until that cleanup settles, so a cancelled close cannot release
        higher-level ownership while a same-group writer remains alive.
        """
        if self._close_task is None:
            self._closed = True
            self._close_task = asyncio.create_task(self._close())
        try:
            await asyncio.shield(self._close_task)
        except asyncio.CancelledError as cancelled:
            from garuda.workspace.settlement import settle_task

            try:
                await settle_task(self._close_task)
            except asyncio.CancelledError as cleanup_cancelled:
                # shield() also raises CancelledError when the shared cleanup
                # task itself was cancelled. That is not a cleanup receipt and
                # must reach strict callers as an ordinary failure they can
                # quarantine, never as caller cancellation that releases the
                # lease.
                raise AcpProtocolError(
                    "ACP cleanup task was cancelled; process-group closure is unproved"
                ) from cleanup_cancelled
            except Exception as exc:
                # Cleanup uncertainty outranks cancellation: strict callers
                # must quarantine ownership instead of releasing on a bare
                # CancelledError whose chained cause is easy to overlook.
                raise exc from cancelled
            raise
        self._close_task.result()

    async def _close(self) -> None:
        process = self._process
        tasks = tuple(
            task for task in (self._reader_task, self._stderr_task) if task is not None
        )
        for task in tasks:
            if not task.done():
                task.cancel()
        try:
            if process is not None:
                await self._close_process_group(process)
                self._close_process_pipes(process)
                try:
                    await asyncio.wait_for(
                        asyncio.shield(process.wait()), PIPE_DRAIN_TIMEOUT
                    )
                except TimeoutError as exc:
                    raise AcpProtocolError(
                        "ACP group closed but its leader could not be reaped"
                    ) from exc
            self._process = None
        finally:
            try:
                if tasks:
                    drain = asyncio.gather(*tasks, return_exceptions=True)
                    try:
                        await asyncio.wait_for(drain, PIPE_DRAIN_TIMEOUT)
                    except TimeoutError as exc:
                        raise AcpProtocolError(
                            "ACP protocol readers did not settle after process cleanup"
                        ) from exc
            finally:
                self._fail_all_pending(AcpCancelledError("process closed"))

    async def _close_process_group(self, process: asyncio.subprocess.Process) -> None:
        if self._process_group is None or self._leader_identity is None:
            if process.returncode is None:
                raise AcpProtocolError("ACP process-group ownership was not recorded")
            return
        await asyncio.to_thread(self._signal_owned_group, signal.SIGTERM)
        if await self._wait_group_gone(TERM_GRACE):
            return
        # Escalation is based on the group's live members, never merely on the
        # leader's return code. A prompt leader exit must not orphan a child.
        await asyncio.to_thread(self._signal_owned_group, signal.SIGKILL)
        if not await self._wait_group_gone(KILL_GRACE):
            members = await asyncio.to_thread(
                self._owned_group_members, self._process_group
            )
            raise AcpProtocolError(
                "ACP process group survived SIGKILL: "
                + ", ".join(str(pid) for pid in members)
            )

    async def _wait_group_gone(self, timeout: float) -> bool:
        assert self._process_group is not None
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            members = await asyncio.to_thread(
                self._owned_group_members, self._process_group
            )
            if not members:
                return True
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(GROUP_POLL_INTERVAL)

    def _owned_group_members(self, process_group: int) -> tuple[int, ...]:
        """Verify that a live group still belongs to this launch, then return it."""
        members = _live_group_members(process_group)
        if not members:
            return ()
        from garuda.runtime.recovery import _process_identity, same_process

        try:
            current_identity = _process_identity(process_group)
        except Exception as exc:
            raise AcpProtocolError(
                f"cannot verify ACP group leader {process_group}: {exc}"
            ) from exc
        if current_identity is not None:
            if not same_process(self._leader_identity, current_identity):
                raise AcpProtocolError(
                    f"ACP group id {process_group} was reused; refusing to signal it"
                )
            try:
                current_group = os.getpgid(process_group)
            except ProcessLookupError:
                # The leader exited between the identity and group probes. Any
                # remaining members still retain the launch's group id.
                return _live_group_members(process_group)
            except OSError as exc:
                raise AcpProtocolError(
                    f"cannot verify ACP group leader {process_group}: {exc}"
                ) from exc
            if current_group != process_group:
                raise AcpProtocolError(
                    f"ACP leader {process_group} joined a foreign process group"
                )
        # If the leader is gone but members remain, POSIX retains its group id;
        # a new group cannot reuse that id until these descendants are gone.
        return members

    def _signal_owned_group(self, sig: signal.Signals) -> None:
        assert self._process_group is not None
        if not self._owned_group_members(self._process_group):
            return
        try:
            os.killpg(self._process_group, sig)
        except ProcessLookupError:
            return
        except OSError as exc:
            raise AcpProtocolError(
                f"could not signal ACP process group {self._process_group}: {exc}"
            ) from exc

    @staticmethod
    def _close_process_pipes(process: asyncio.subprocess.Process) -> None:
        """Close local transports after group proof; detached holders cannot hang us."""
        transport = getattr(process, "_transport", None)
        if transport is None:
            return
        for fd in (0, 1, 2):
            pipe = transport.get_pipe_transport(fd)
            if pipe is not None:
                pipe.close()

    async def __aenter__(self) -> "AcpProcess":
        await self.launch()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()
