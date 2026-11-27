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
#: The most characters of a responder's non-answer reason relayed to a role child.
MAX_UNANSWERED_REASON = 1_024
#: The most characters of a worker's typed refusal message (#738).
MAX_REFUSAL_MESSAGE = 2_048
#: Code namespaces a role worker may report as its typed refusal: the role
#: pipeline's own (``RoleRefused``), never a flow, workspace or check code.
REFUSAL_NAMESPACES = ("role", "harness", "config")


def refusal_code(code) -> bool:
    """A typed refusal code a role worker may report, e.g. ``role.start_failed``."""
    if not isinstance(code, str) or len(code) > 64 or not code.isascii():
        return False
    namespace, _, name = code.partition(".")
    return namespace in REFUSAL_NAMESPACES and bool(name) and all(
        part and part.replace("_", "").isalnum() and part == part.lower() for part in name.split("."))


def valid_refusal(refusal) -> bool:
    """A worker's typed refusal: ``{"code": "<namespace>.<name>", "message": str}``."""
    return (isinstance(refusal, dict) and set(refusal) == {"code", "message"}
            and refusal_code(refusal["code"]) and isinstance(refusal["message"], str)
            and len(refusal["message"]) <= MAX_REFUSAL_MESSAGE)


#: Why a worker's agent stopped (#828): keys a typed ``failure`` may carry.
FAILURE_KEYS = frozenset({"code", "detail", "stop_reason", "liveness"})


def valid_failure(failure) -> bool:
    """A worker's typed failure cause: a bounded code, detail and raw stop reason,
    and which of the step's own limits (``deadline``/``silence``) stopped it."""
    def text(value, limit):
        return value is None or (isinstance(value, str) and len(value) <= limit)

    return (isinstance(failure, dict) and set(failure) <= FAILURE_KEYS
            and text(failure.get("code"), 120) and text(failure.get("detail"), MAX_REFUSAL_MESSAGE)
            and text(failure.get("stop_reason"), 64)
            and failure.get("liveness") in (None, "deadline", "silence"))


def valid_turns(record) -> bool:
    """A worker's turn record (:meth:`garuda.flows.long_step.StepTurns.record`), bounded."""
    from garuda.flows.long_step import valid_record

    return valid_record(record)


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
        #: The worker asked to retain ownership; a result-less exit then stays quarantined.
        self.quarantine_requested = False
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
                reply = await self._respond(request["op"], request["value"])
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

    async def _respond(self, operation, value):
        try:
            response = await self._dispatch(operation, value)
        except Exception:
            return {"ok": False, "value": "role-control-refused"}
        from garuda.core.approval_outcome import Unanswered

        if isinstance(response, Unanswered):
            # The value stays a "no"; the extra field carries why nobody answered,
            # and a peer that does not expect it refuses the reply and denies.
            reason = response.reason if isinstance(response.reason, str) else None
            return {"ok": True, "value": False, "unanswered": {
                "outcome": response.outcome.value,
                "reason": reason[:MAX_UNANSWERED_REASON] if reason is not None else None}}
        return {"ok": True, "value": response}

    async def _dispatch(self, operation, value):
        if operation == "quarantine" and value is None:
            self.quarantine_requested = True
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
            if self.approval is None:
                return False
            from garuda.core.approval_outcome import Unanswered

            answer = await self.approval(value)
            # A timeout or lost person stays a non-answer for the worker's broker to audit.
            return answer if isinstance(answer, Unanswered) else bool(answer)
        if operation == "result" and self.result is None:
            if (not isinstance(value, dict)
                    or set(value) - {"refusal", "failure", "turns"} != {"session_id", "success", "output"}
                    or {"refusal", "failure"} <= set(value)
                    or not isinstance(value["session_id"], str)
                    or type(value["success"]) is not bool or not isinstance(value["output"], str)
                    or len(value["output"].encode()) > MAX_RESULT_OUTPUT
                    # A typed refusal (#738) is only ever a failed step with no output.
                    or ("refusal" in value and (not valid_refusal(value["refusal"])
                                                or value["success"] is not False or value["output"]))
                    # A typed failure cause (#828) only ever describes a failed step.
                    or ("failure" in value and (not valid_failure(value["failure"])
                                                or value["success"] is not False))
                    # What an ACP step's turns did (repair turn, turn loop); never with a refusal.
                    or ("turns" in value and ("refusal" in value or not valid_turns(value["turns"])))):
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
        return self._exchange(op, value)["value"]

    def _exchange(self, op, value, *, unanswered=False):
        message = {"nonce": self.nonce, "sequence": self.sequence, "op": op, "value": value}
        raw = json.dumps(message, allow_nan=False).encode() + b"\n"
        if len(raw) > CONTROL_LIMIT:
            raise LeaseError("oversized role control request")
        self._socket.sendall(raw)
        self.sequence += 1
        reply = json.loads(self._reader.readline(CONTROL_LIMIT + 1))
        shapes = ({"ok", "value"}, {"ok", "value", "unanswered"}) if unanswered else ({"ok", "value"},)
        if not isinstance(reply, dict) or set(reply) not in shapes or reply["ok"] is not True:
            raise LeaseError("role control refused or disconnected")
        return reply

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
        """The parent's yes or no, or an :class:`~garuda.core.approval_outcome.Unanswered`
        (falsy) when its responder timed out or lost its person."""
        from garuda.core.approval_outcome import ApprovalOutcome, Unanswered

        reply = self._exchange("approve", action, unanswered=True)
        if type(reply["value"]) is not bool:
            raise LeaseError("role approval is neither a yes nor a no")
        if "unanswered" not in reply:
            return reply["value"]
        detail = reply["unanswered"]
        if (reply["value"] is not False or not isinstance(detail, dict)
                or set(detail) != {"outcome", "reason"}
                or not (detail["reason"] is None or isinstance(detail["reason"], str))):
            raise LeaseError("invalid role approval non-answer")
        try:
            return Unanswered(ApprovalOutcome(detail["outcome"]), detail["reason"])
        except ValueError:
            raise LeaseError("invalid role approval non-answer") from None

    def close(self):
        self._reader.close()
        self._socket.close()
