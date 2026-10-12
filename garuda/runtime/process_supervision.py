"""Owned Linux subprocess trees, including descendants which leave their group.

The private worker becomes a subreaper before acknowledging preparation. No
command is sent until the caller has durably authorized activation. This is
process ownership, not confinement; work delegated to existing daemons is outside
the tree. Missing worker evidence always raises uncertainty.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import signal
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

WIRE_LIMIT = 65_536
TAIL_BYTES = 4_096  # Two maximally escaped tails still fit the bounded JSON channel.


class SupervisionUnavailable(Exception):
    """The requested execution cannot be supervised; no command was authorized."""


class CleanupUncertain(Exception):
    """Possible owned descendants remain; the caller must retain quarantine."""


def _identity(value):
    return (isinstance(value, dict) and set(value) == {"pid", "identity", "pgid"}
            and type(value["pid"]) is int and value["pid"] > 1
            and type(value["pgid"]) is int and value["pgid"] == value["pid"]
            and isinstance(value["identity"], str) and value["identity"] not in {"", "unknown"})


@dataclass(frozen=True)
class ProcessRequest:
    run: str | tuple[str, ...]
    cwd: str
    env: tuple[tuple[str, str], ...] = ()
    timeout: float = 900.0
    term_grace: float = 3.0
    kill_grace: float = 3.0
    input_data: str | None = None

    def document(self) -> dict:
        if not isinstance(self.run, (str, tuple)) or not self.run:
            raise ValueError("command must be nonempty shell text or an argv tuple")
        if isinstance(self.run, tuple) and (not self.run[0] or any(not isinstance(v, str) for v in self.run)):
            raise ValueError("argv must contain strings and a nonempty executable")
        if not isinstance(self.cwd, str) or not os.path.isabs(self.cwd):
            raise ValueError("cwd must be absolute")
        for value, maximum in ((self.timeout, 900), (self.term_grace, 3), (self.kill_grace, 3)):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not (
                    math.isfinite(value) and 0 < value <= maximum):
                raise ValueError("process limits must be positive and within policy bounds")
        env = dict(self.env)
        if len(env) != len(self.env) or any(not isinstance(k, str) or not k or "=" in k
                                         or not isinstance(v, str) for k, v in self.env):
            raise ValueError("invalid environment definitions")
        if self.input_data is not None and not isinstance(self.input_data, str):
            raise ValueError("owned helper input must be text")
        result = {"version": 1, "run": self.run, "cwd": self.cwd, "env": env,
                  "timeout": self.timeout, "term_grace": self.term_grace,
                  "kill_grace": self.kill_grace, "input_data": self.input_data}
        if len(json.dumps(result).encode()) > WIRE_LIMIT - 1:
            raise ValueError("process request exceeds its bound")
        return result


@dataclass(frozen=True)
class ProcessResult:
    supervisor: dict
    command: dict | None
    exit_code: int | None
    outcome: str
    stdout_tail: str
    stderr_tail: str
    cleanup: dict
    duration_s: float


class ProcessSupervisor(Protocol):
    async def prepare(self) -> OwnedProcess: ...


class LinuxProcessSupervisor:
    async def prepare(self) -> OwnedProcess:
        if sys.platform != "linux" or not hasattr(os, "pidfd_open") or not hasattr(
                signal, "pidfd_send_signal"):
            raise SupervisionUnavailable("Linux subreaper and pidfd supervision is required")
        launch_id = str(uuid.uuid4())
        # Ignore interpreter environment/path injection while importing the
        # installed harness. The check itself retains its requested environment.
        bootstrap = ("import runpy,sys; sys.path.insert(0,sys.argv[1]); "
                     "sys.argv=sys.argv[2:]; "
                     "runpy.run_module('garuda.runtime.process_worker',run_name='__main__')")
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-I", "-c", bootstrap, str(Path(__file__).resolve().parents[2]),
            "garuda.runtime.process_worker", launch_id,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, limit=WIRE_LIMIT, start_new_session=True)
        owned = OwnedProcess(proc, launch_id)
        try:
            owned._pidfd = os.pidfd_open(proc.pid)
            owned._preparer = asyncio.create_task(owned._ready())
            await asyncio.shield(owned._preparer)
            return owned
        except BaseException:
            await owned._settle_stop()
            raise


class OwnedProcess:
    def __init__(self, proc, launch_id):
        self.proc, self.launch_id = proc, launch_id
        self.supervisor: dict = {}
        self.command: dict | None = None
        self.result: ProcessResult | None = None
        self._pidfd: int | None = None
        self._request: ProcessRequest | None = None
        self._reader: asyncio.Task | None = None
        self._stopper: asyncio.Task | None = None
        self._preparer: asyncio.Task | None = None
        self._activator: asyncio.Task | None = None

    async def _ready(self):
        from garuda.runtime.recovery import _process_identity

        message = await asyncio.wait_for(self._message(), 5)
        owner = message.get("supervisor")
        if (set(message) != {"version", "launch_id", "kind", "supervisor"}
                or message.get("kind") != "ready" or not _identity(owner)
                or owner.get("pid") != self.proc.pid
                or owner.get("identity") != _process_identity(self.proc.pid)
                or not owner.get("identity")):
            raise CleanupUncertain("supervisor preparation is not identity-bound")
        self.supervisor = owner

    async def _message(self) -> dict:
        try:
            raw = await self.proc.stdout.readline()
            message = json.loads(raw)
            if (len(raw) > WIRE_LIMIT or not isinstance(message, dict)
                    or type(message.get("version")) is not int or message["version"] != 1
                    or message.get("launch_id") != self.launch_id):
                raise ValueError("invalid worker envelope")
            return message
        except (ValueError, UnicodeError) as exc:
            raise CleanupUncertain("missing or invalid supervisor evidence") from exc

    async def activate(self, request: ProcessRequest) -> dict | None:
        """Caller must publish launch authorization before invoking this method."""
        if self._request is not None or self._reader is not None:
            raise ValueError("a supervisor can authorize only one command")
        document = request.document()
        self._request = request
        self._activator = asyncio.create_task(asyncio.wait_for(self._activate(document), 5))
        try:
            return await asyncio.shield(self._activator)
        except BaseException:
            await self._settle_stop()
            raise

    async def _activate(self, document):
        self.proc.stdin.write(json.dumps(document).encode() + b"\n")
        await self.proc.stdin.drain()
        message = await self._message()
        command = message.get("command")
        if message.get("kind") == "terminal" and command is None:
            self._reader = asyncio.create_task(self._terminal(message))
            await asyncio.shield(self._reader)
            return None
        if (set(message) != {"version", "launch_id", "kind", "command"}
                or message.get("kind") != "started" or not _identity(command)):
            raise CleanupUncertain("command activation lacks process identity")
        self.command = command
        self._reader = asyncio.create_task(self._terminal())
        return command

    async def _terminal(self, message=None) -> ProcessResult:
        message = message if message is not None else await self._message()
        cleanup = message.get("cleanup")
        if (set(message) != {"version", "launch_id", "kind", "supervisor", "command", "outcome",
                            "exit_code", "cleanup", "duration_s", "stdout_tail", "stderr_tail"}
                or message.get("kind") != "terminal" or not _identity(message.get("supervisor"))
                or message.get("supervisor") != self.supervisor
                or (message.get("command") is not None and not _identity(message["command"]))
                or message.get("command") != self.command or not isinstance(cleanup, dict)
                or set(cleanup) != {"backend", "echild"}
                or cleanup.get("backend") != "linux-subreaper" or cleanup.get("echild") is not True
                or message.get("outcome") not in {"exited", "timeout", "cancelled", "launch-failed"}
                or any(not isinstance(message.get(k), str) or len(message[k]) > TAIL_BYTES
                       for k in ("stdout_tail", "stderr_tail"))):
            raise CleanupUncertain("terminal evidence does not prove subtree closure")
        code = message.get("exit_code")
        duration = message.get("duration_s")
        if ((code is not None and type(code) is not int) or type(duration) not in (int, float)
                or not math.isfinite(duration) or duration < 0
                or (message["outcome"] == "exited" and code is None)
                or (self.command is None and message["outcome"] in {"exited", "timeout"})):
            raise CleanupUncertain("invalid terminal execution facts")
        if await self.proc.wait() != 0:
            raise CleanupUncertain("supervisor failed after terminal publication")
        self.result = ProcessResult(self.supervisor, self.command, code, message["outcome"],
                                    message["stdout_tail"], message["stderr_tail"], cleanup, duration)
        return self.result

    async def wait(self) -> ProcessResult:
        if self._reader is None:
            raise ValueError("command has not been activated")
        try:
            bound = self._request.timeout + self._request.term_grace + self._request.kill_grace + 2
            return await asyncio.wait_for(asyncio.shield(self._reader), bound)
        except BaseException:
            await self._settle_stop()
            raise

    async def _stop(self) -> None:
        grace = (self._request.term_grace + self._request.kill_grace
                 if self._request else 6) + 2
        deadline = None
        try:
            if self._preparer is not None:
                await self._preparer
            if self._activator is not None:
                await self._activator
            self.proc.stdin.close()  # EOF asks the ready worker to reap its subtree.
            deadline = asyncio.get_running_loop().time() + grace
            if self._reader is None:
                self._reader = asyncio.create_task(self._terminal())
            await asyncio.wait_for(asyncio.shield(self._reader), grace)
        except BaseException as exc:
            self.proc.stdin.close()
            remaining = grace if deadline is None else max(0, deadline - asyncio.get_running_loop().time())
            try:
                await asyncio.wait_for(self.proc.wait(), remaining)
            except TimeoutError:
                pass
            # Kill only our pidfd-bound supervisor. Its lost evidence never
            # authorizes a claim that detached descendants have stopped.
            if self.supervisor and self._pidfd is not None and self.proc.returncode is None:
                try:
                    signal.pidfd_send_signal(self._pidfd, signal.SIGKILL)
                except OSError:
                    pass
            try:
                await asyncio.wait_for(self.proc.wait(), 2)
            except TimeoutError:
                pass
            if self._reader is not None and not self._reader.done():
                self._reader.cancel()
                await asyncio.gather(self._reader, return_exceptions=True)
            raise CleanupUncertain("supervisor did not establish bounded cleanup") from exc
        finally:
            if self._pidfd is not None:
                os.close(self._pidfd)
                self._pidfd = None

    async def _settle_stop(self) -> None:
        if self._stopper is None:
            self._stopper = asyncio.create_task(self._stop())
        while True:
            try:
                await asyncio.shield(self._stopper)
                return
            except asyncio.CancelledError:
                if self._stopper.done():
                    self._stopper.result()
                    return

    async def close(self) -> None:
        await self._settle_stop()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        await self.close()
