"""ACP subprocess lifecycle over stdio (P0.12, issue #21).

`AcpProcess` launches one configured agent process and speaks the ACP wire
subset: initialize, session/new, session/prompt, session/cancel, with
session/update notifications streamed to a queue. stdin/stdout carry framed
protocol bytes only; stderr is drained separately into a bounded diagnostics
ring that never touches the frame parser.

Safety posture: the argv comes from the caller (the registry-resolved
allowlist, wired by a later adapter issue) — this module never searches PATH
beyond what execvp already does and never inherits the parent environment.
The child gets a minimal explicit environment plus caller-supplied extras, so
no secret crosses the boundary by accident. Processes launch in their own
session (`start_new_session`) and close() always reaps: SIGTERM to the group,
a grace period, SIGKILL, then wait.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from collections import deque
from typing import Any

from garuda.acp.protocol import (
    ACP_VERSION,
    MAX_FRAME_BYTES,
    MAX_HEADER_BYTES,
    AcpCancelledError,
    AcpExitError,
    AcpProtocolError,
    AcpTimeoutError,
    decode_frame,
    encode_frame,
)

logger = logging.getLogger(__name__)

HANDSHAKE_TIMEOUT = 30.0
CALL_TIMEOUT = 120.0
TERM_GRACE = 5.0
STDERR_RING = 50


class AcpProcess:
    """One managed ACP agent subprocess."""

    def __init__(
        self,
        argv: list[str],
        *,
        extra_env: dict[str, str] | None = None,
        call_timeout: float = CALL_TIMEOUT,
    ):
        if not argv or any(not isinstance(p, str) or not p for p in argv):
            raise AcpProtocolError("argv must be a non-empty list of strings")
        self._argv = list(argv)
        self._extra_env = dict(extra_env or {})
        self._call_timeout = call_timeout
        self._process: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task | None = None
        self._stderr_task: asyncio.Task | None = None
        self._buffer = b""
        self._next_id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._notifications: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._stderr_ring: deque[str] = deque(maxlen=STDERR_RING)
        self._closed = False

    @property
    def pid(self) -> int | None:
        return self._process.pid if self._process else None

    @property
    def stderr_tail(self) -> str:
        return "\n".join(self._stderr_ring)

    def _child_env(self) -> dict[str, str]:
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": os.environ.get("HOME", ""),
            "LANG": "C.UTF-8",
        }
        env.update(self._extra_env)
        return {k: v for k, v in env.items() if v}

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
            start_new_session=True,
        )
        self._reader_task = asyncio.ensure_future(self._read_loop())
        self._stderr_task = asyncio.ensure_future(self._drain_stderr())

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
                        pass
                    self._on_eof()
                    return
                self._buffer += chunk
                if len(self._buffer) > MAX_FRAME_BYTES + MAX_HEADER_BYTES:
                    self._on_transport_error(
                        AcpProtocolError("reader buffer exceeds maximum frame size")
                    )
                    return
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
            future = self._pending.pop(message["id"], None)
            if future is not None and not future.done():
                if "error" in message:
                    error = message["error"] or {}
                    future.set_exception(
                        AcpProtocolError(
                            f"agent error {error.get('code')}: {error.get('message')}"
                        )
                    )
                else:
                    future.set_result(message.get("result"))
        elif message.get("method"):
            self._notifications.put_nowait(message)
        else:
            self._on_transport_error(AcpProtocolError(f"unroutable message: {message!r}"))

    def _fail_all_pending(self, exc: Exception) -> None:
        pending, self._pending = self._pending, {}
        for future in pending.values():
            if not future.done():
                future.set_exception(exc)

    def _on_eof(self) -> None:
        code = self._process.returncode if self._process else None
        self._fail_all_pending(
            AcpExitError(
                f"agent exited (code={code}); stderr: {self.stderr_tail[-500:]}",
                exit_code=code,
                stderr_tail=self.stderr_tail,
            )
        )

    def _on_transport_error(self, exc: Exception) -> None:
        self._fail_all_pending(exc)

    async def _call(
        self, method: str, params: dict[str, Any], *, timeout: float | None = None
    ) -> Any:
        if self._process is None or self._process.stdin is None:
            raise AcpProtocolError("process is not running")
        if self._process.returncode is not None:
            raise AcpExitError(
                f"agent already exited (code={self._process.returncode})",
                exit_code=self._process.returncode,
                stderr_tail=self.stderr_tail,
            )
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
            return await asyncio.wait_for(future, timeout or self._call_timeout)
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
        if self._process is None or self._process.stdin is None:
            raise AcpProtocolError("process is not running")
        try:
            self._process.stdin.write(
                encode_frame({"jsonrpc": "2.0", "method": method, "params": params})
            )
            await self._process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as exc:
            raise AcpExitError(f"agent stdin broken: {exc}", exit_code=None) from exc

    async def initialize(self, *, timeout: float = HANDSHAKE_TIMEOUT) -> dict[str, Any]:
        """Handshake. A version mismatch fails here, never mid-session."""
        result = await self._call(
            "initialize",
            {"protocolVersion": ACP_VERSION, "clientInfo": {"name": "garuda"}},
            timeout=timeout,
        )
        if not isinstance(result, dict):
            raise AcpProtocolError("initialize result must be an object")
        return result

    async def session_new(self, *, timeout: float = HANDSHAKE_TIMEOUT) -> str:
        result = await self._call("session/new", {}, timeout=timeout)
        session_id = result.get("sessionId") if isinstance(result, dict) else None
        if not session_id:
            raise AcpProtocolError("session/new returned no sessionId")
        return session_id

    async def session_prompt(
        self, session_id: str, text: str, *, timeout: float | None = None
    ) -> Any:
        return await self._call(
            "session/prompt",
            {"sessionId": session_id, "prompt": text},
            timeout=timeout,
        )

    async def session_cancel(self, session_id: str) -> None:
        """Cancel a prompt: notify the agent and fail local pending calls."""
        try:
            await self._notify("session/cancel", {"sessionId": session_id})
        finally:
            self._fail_all_pending(AcpCancelledError("cancelled by caller"))

    async def next_notification(self) -> dict[str, Any]:
        return await self._notifications.get()

    def drain_notifications(self) -> list[dict[str, Any]]:
        out = []
        while not self._notifications.empty():
            out.append(self._notifications.get_nowait())
        return out

    @property
    def is_running(self) -> bool:
        return self._process is not None and self._process.returncode is None

    async def close(self) -> None:
        """Terminate the group, escalate, and reap. Idempotent and total."""
        if self._closed:
            if self._process is not None:
                await self._process.wait()
            return
        self._closed = True
        for task in (self._reader_task, self._stderr_task):
            if task is not None and not task.done():
                task.cancel()
        process, self._process = self._process, None
        if process is None:
            return
        if process.returncode is None:
            self._kill_group(process.pid)
            try:
                await asyncio.wait_for(process.wait(), TERM_GRACE)
            except TimeoutError:
                self._kill_group(process.pid, force=True)
                await process.wait()
        else:
            await process.wait()
        for task in (self._reader_task, self._stderr_task):
            if task is not None:
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
        self._fail_all_pending(AcpCancelledError("process closed"))

    def _kill_group(self, pid: int, *, force: bool = False) -> None:
        sig = signal.SIGKILL if force else signal.SIGTERM
        try:
            os.killpg(pid, sig)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                os.kill(pid, sig)
            except (ProcessLookupError, PermissionError, OSError):
                pass

    async def __aenter__(self) -> "AcpProcess":
        await self.launch()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()
