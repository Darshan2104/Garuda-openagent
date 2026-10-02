"""The session-local consult endpoint for an ACP asker (plan task G.3, #170).

One broker per asking session. It listens on a Unix socket inside a private (``0700``)
directory and answers exactly one operation, ``consult``, for the one session it was built
for. A request must carry the random 256-bit token minted for the current epoch; the asker,
root, workspace and ceilings come from the broker, never from the request. Resuming the
session rotates the epoch, so an earlier token stops working.

This is a local-process boundary, not protection from a hostile process running as the same
user: such a process can read the asker's environment and files anyway. Owner-only
permissions keep other users out; nothing here claims more.

The token is registered with the redactor before it is placed anywhere, is never written to
the store, a log or the child's environment, and a rejected token is never echoed.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import os
import secrets
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from garuda.consult.errors import ConsultRefused

MAX_REQUEST_BYTES = 128 * 1024
READ_TIMEOUT_SEC = 10.0
_FIELDS = frozenset({"token", "request_id", "target", "question", "brief"})


@dataclass(frozen=True)
class Endpoint:
    path: str
    token: str
    epoch: int


class ConsultBroker:
    def __init__(self, service, *, asker_session: str, root_session: str, workspace: str,
                 pid: int, quiesce=None, identity=None, source_turn=None):
        self._service = service
        self._asker = asker_session
        self._root = root_session
        self._workspace = workspace
        self._pid = pid
        self._quiesce = quiesce
        self._identity = identity if identity is not None else self._probe()
        self._token = ""
        self._epoch = 0
        self._dir: Path | None = None
        self._server: asyncio.AbstractServer | None = None
        self._closed = False

    # --- lifecycle ------------------------------------------------------------------------

    async def start(self) -> Endpoint:
        if self._server is not None:
            raise RuntimeError("the consult broker is already running")
        base = tempfile.gettempdir()
        if len(base) > 60:  # AF_UNIX paths are short (104 bytes on macOS)
            base = "/tmp"
        self._dir = Path(tempfile.mkdtemp(prefix="gcb-", dir=base))  # created 0700
        os.chmod(self._dir, 0o700)
        path = str(self._dir / "s")
        self._server = await asyncio.start_unix_server(self._serve, path=path,
                                                       limit=MAX_REQUEST_BYTES)
        os.chmod(path, 0o600)
        return self.rotate()

    def rotate(self) -> Endpoint:
        """A new epoch: the previous token no longer works. Registered before it is returned."""
        from garuda.context.redact import forget_secret, register_secret

        if self._token:
            forget_secret(self._token)
        token = secrets.token_urlsafe(32)  # 256 bits
        register_secret(token)
        self._token = token
        self._epoch += 1
        return Endpoint(str(self._dir / "s"), token, self._epoch)

    @property
    def epoch(self) -> int:
        return self._epoch

    @property
    def socket_path(self) -> str:
        return str(self._dir / "s") if self._dir else ""

    async def close(self) -> None:
        from garuda.context.redact import forget_secret

        self._closed = True
        if self._token:
            forget_secret(self._token)
        self._token = ""
        server, self._server = self._server, None
        if server is not None:
            server.close()
            with contextlib.suppress(Exception):
                await server.wait_closed()
        if self._dir is not None:
            shutil.rmtree(self._dir, ignore_errors=True)
            self._dir = None

    # --- one connection ---------------------------------------------------------------------

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            reply = await self._handle(reader)
        except asyncio.CancelledError:
            raise
        except Exception:  # never leak an internal message to the client
            reply = {"ok": False, "code": "consult.failed", "message": "the consult failed"}
        with contextlib.suppress(Exception):
            writer.write((json.dumps(reply) + "\n").encode("utf-8"))
            await writer.drain()
        with contextlib.suppress(Exception):
            writer.close()
            await writer.wait_closed()

    async def _handle(self, reader: asyncio.StreamReader) -> dict:
        try:
            line = await asyncio.wait_for(reader.readline(), READ_TIMEOUT_SEC)
            request = json.loads(line.decode("utf-8"))
        except (asyncio.TimeoutError, ValueError, UnicodeDecodeError, asyncio.LimitOverrunError):
            return _refusal("consult.invalid", "the request was not valid")
        if not isinstance(request, dict):
            return _refusal("consult.invalid", "the request was not valid")
        presented = request.get("token")
        if (self._closed or not isinstance(presented, str) or not self._token
                or not hmac.compare_digest(presented.encode(), self._token.encode())):
            # one message for a wrong, old-epoch or missing token: nothing to learn from it
            return _refusal("consult.unauthenticated", "the consult endpoint refused the request")
        if set(request) - _FIELDS:
            return _refusal("consult.invalid", "a request names only target, question, brief "
                                               "and request_id; the session is fixed")
        if not self._asker_alive():
            return _refusal("consult.invalid", "the asking session has ended")
        if self._quiesce is None:
            return _refusal("consult.snapshot_unsupported",
                            "this adapter has no proved handshake to pause its workspace")
        fields = {name: request.get(name) for name in ("request_id", "target", "question", "brief")}
        if any(value is not None and not isinstance(value, str) for value in fields.values()):
            return _refusal("consult.invalid", "fields must be strings")
        from garuda.consult.service import ConsultRequest

        req = ConsultRequest(
            asker_session=self._asker, root_session=self._root, target=fields["target"] or "",
            question=fields["question"] or "", brief=fields["brief"] or "",
            request_id=fields["request_id"] or "", workspace=self._workspace)
        try:
            result = await self._service.consult(req, quiesce=self._quiesce)
        except ConsultRefused as exc:
            return _refusal(exc.code, exc.message)
        if result.outcome != "answered":
            return _refusal(result.code or "consult.failed", result.message or "no answer")
        return {"ok": True, "text": result.text, "replay": bool(result.replay)}

    # --- the asker's liveness ----------------------------------------------------------------

    def _probe(self):
        try:
            from garuda.runtime.recovery import _process_identity

            return _process_identity(self._pid)
        except Exception:
            return None

    def _asker_alive(self) -> bool:
        """Still the same process that was registered; unknown means no."""
        if self._identity is None:
            return False
        try:
            return self._probe() == self._identity
        except Exception:
            return False


def _refusal(code: str, message: str) -> dict:
    return {"ok": False, "code": code, "message": message}
