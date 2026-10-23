"""Private control for an owned role; the retained parent issues every lease action."""

from __future__ import annotations

import asyncio
import hmac
import json
import os
import secrets
import socket
import struct
import tempfile
from pathlib import Path

from garuda.interfaces.run_guard import LeaseCapability
from garuda.runtime.process_supervision import CleanupUncertain
from garuda.workspace.lease import LeaseError

CONTROL_LIMIT = 65_536
#: The most bytes of step output a role child may hand back.
MAX_RESULT_OUTPUT = 32_768


def bounded_output(text: str) -> str:
    """The end of ``text`` within :data:`MAX_RESULT_OUTPUT` bytes: flow artifact blocks
    close an answer, so the tail is what a step's outputs come from."""
    data = text.encode()
    if len(data) <= MAX_RESULT_OUTPUT:
        return text
    return data[-MAX_RESULT_OUTPUT:].decode(errors="ignore")


class RoleControl:
    """Keep the actual issued capability and capacity in the parent process.

    The child can validate, request approval, and report its result. Its release
    request is logical only: the caller releases the retained guard after ECHILD.
    Socket peer credentials must identify the actual supervisor-issued command.
    """

    def __init__(self, capability, workspace, capacity_key, *, approval=None, capacity_policy=None,
                 readonly=False):
        if type(capability) is not LeaseCapability:
            raise LeaseError("owned roles require an issued parent capability")
        if capability._parent._mode != "mutating" and not readonly:
            raise LeaseError("owned roles require a mutating parent admission")
        if os.path.realpath(workspace) != os.path.realpath(capability.workspace):
            raise LeaseError("owned role workspace does not match its capability")
        self.guard = capability.guard(capacity_key=capacity_key, capacity_policy=capacity_policy)
        self.parent_snapshot = capability._parent.snapshot_lifetime()
        if self.parent_snapshot["lease"]["mode"] not in (
                {"mutating", "read-only"} if readonly else {"mutating"}):
            raise LeaseError("owned role posture disagrees with its retained issuance")
        self.capacity_key = capacity_key
        self.workspace = os.path.realpath(workspace)
        self.approval = approval
        self.nonce = secrets.token_hex(32)
        self.result = None
        self._process = None
        self._active = asyncio.Event()
        self._connected = False
        self._writers = set()
        self._handlers = set()
        self._directory = None
        self._server = None
        self._released = False
        self._closer = None

    async def start(self, process):
        self.guard.acquire()
        self._process = process
        try:
            self._directory = tempfile.TemporaryDirectory(prefix="garuda-role-")
            os.chmod(self._directory.name, 0o700)
            self.path = str(Path(self._directory.name) / "control.sock")
            self._server = await asyncio.start_unix_server(
                self._connection, path=self.path, limit=CONTROL_LIMIT)
            os.chmod(self.path, 0o600)
        except BaseException:
            await self.guard.release()
            if self._directory is not None:
                self._directory.cleanup()
            raise
        return {"path": self.path, "nonce": self.nonce, "workspace": self.workspace,
                "capacity_key": self.capacity_key}

    def activated(self):
        self._active.set()

    async def _connection(self, reader, writer):
        task = asyncio.current_task()
        self._handlers.add(task)
        self._writers.add(writer)
        try:
            peer = writer.get_extra_info("socket")
            pid, uid, _gid = struct.unpack("3i", peer.getsockopt(
                socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")))
            await asyncio.wait_for(self._active.wait(), 6)
            from garuda.runtime.recovery import _process_identity

            command = self._process.command
            if (not command or pid != command["pid"] or uid != os.getuid()
                    or _process_identity(pid) != command["identity"] or self._connected):
                return
            self._connected = True
            sequence = 0
            while True:
                raw = await reader.readline()
                if not raw:
                    return
                if len(raw) > CONTROL_LIMIT:
                    raise ValueError("oversized control request")
                request = json.loads(raw)
                if (not isinstance(request, dict)
                        or set(request) != {"nonce", "sequence", "op", "value"}
                        or not isinstance(request["nonce"], str)
                        or not hmac.compare_digest(request["nonce"], self.nonce)
                        or type(request["sequence"]) is not int
                        or request["sequence"] != sequence):
                    raise ValueError("invalid control request")
                sequence += 1
                try:
                    response = await self._dispatch(request["op"], request["value"])
                    reply = {"ok": True, "value": response}
                except Exception:
                    reply = {"ok": False, "value": "role-control-refused"}
                writer.write(json.dumps(reply, allow_nan=False).encode() + b"\n")
                await writer.drain()
        except (Exception, asyncio.CancelledError):
            # An incomplete channel never authorizes a result or physical release.
            pass
        finally:
            writer.close()
            await writer.wait_closed()
            self._writers.discard(writer)
            self._handlers.discard(task)

    async def _dispatch(self, operation, value):
        if operation == "quarantine" and value is None:
            await self.guard.stop_heartbeat()
            return None
        self.guard._check()
        if operation == "acquire" and value == self.capacity_key and not self._released:
            return None
        if operation == "check" and value is None and not self._released:
            return None
        if operation == "release" and value is None and not self._released:
            self._released = True
            return None
        if operation == "approve" and isinstance(value, str) and len(value) <= 16_384:
            if self._released:
                raise LeaseError("role already released")
            return bool(await self.approval(value)) if self.approval else False
        if operation == "result" and self.result is None:
            if (not isinstance(value, dict) or set(value) != {"session_id", "success", "output"}
                    or not isinstance(value["session_id"], str)
                    or type(value["success"]) is not bool or not isinstance(value["output"], str)
                    or len(value["output"].encode()) > MAX_RESULT_OUTPUT):
                raise ValueError("invalid role result")
            from garuda.core.sessions import validate_session_ref

            validate_session_ref(value["session_id"])
            self.result = value
            return None
        raise LeaseError("role control operation is not admitted")

    async def close(self, *, closed):
        if self._closer is None:
            self._closer = asyncio.create_task(self._close(closed=closed))
        while True:
            try:
                return await asyncio.shield(self._closer)
            except asyncio.CancelledError:
                if self._closer.done():
                    return self._closer.result()

    async def _close(self, *, closed):
        try:
            await self._close_resources(closed=closed)
        except BaseException:
            await self.guard.stop_heartbeat()
            raise

    async def _close_resources(self, *, closed):
        if not closed:
            await self.guard.stop_heartbeat()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
        for writer in list(self._writers):
            writer.close()
        pending = list(self._handlers)
        for task in pending:
            task.cancel()
        if pending:
            _done, unfinished = await asyncio.wait(pending, timeout=2)
            if unfinished:
                await self.guard.stop_heartbeat()
                raise CleanupUncertain("role control did not settle")
        if self._directory is not None:
            self._directory.cleanup()
        # BorrowedLease.release never releases the parent's workspace. Unknown
        # closure or a worker-requested quarantine also retains its capacity.
        await self.guard.release()


class RoleControlClient:
    """Worker-only proxy. Contains no LeaseStore and cannot mint a lease grant."""

    def __init__(self, document):
        self.workspace = document["workspace"]
        self.capacity_key = document["capacity_key"]
        self.nonce = document["nonce"]
        self.sequence = 0
        self._socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._socket.settimeout(10)
        self._socket.connect(document["path"])
        self._reader = self._socket.makefile("rb")

    def request(self, op, value=None):
        message = {"nonce": self.nonce, "sequence": self.sequence, "op": op, "value": value}
        raw = json.dumps(message, allow_nan=False).encode() + b"\n"
        if len(raw) > CONTROL_LIMIT:
            raise LeaseError("oversized role control request")
        self._socket.sendall(raw)
        self.sequence += 1
        reply = json.loads(self._reader.readline(CONTROL_LIMIT + 1))
        if (not isinstance(reply, dict) or set(reply) != {"ok", "value"}
                or reply["ok"] is not True):
            raise LeaseError("role control refused or disconnected")
        return reply["value"]

    def guard(self, *, capacity_key=None):
        if capacity_key != self.capacity_key:
            raise LeaseError("role capacity does not match its admitted runtime")
        return self

    def acquire(self):
        self.request("acquire", self.capacity_key)

    def start_heartbeat(self):
        self.request("check")

    async def race(self, work):
        try:
            self.request("check")
        except BaseException:
            if hasattr(work, "close"):
                work.close()
            raise
        return await work

    async def stop_heartbeat(self):
        self.request("quarantine")

    async def release(self):
        self.request("release")

    async def approve(self, action):
        return self.request("approve", action)

    def close(self):
        self._reader.close()
        self._socket.close()
