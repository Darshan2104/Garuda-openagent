"""A run-owned language server spoken to over stdio JSON-RPC.

Lifecycle: constructing a :class:`StdioLanguageServer` launches nothing. The
first request starts the process in the workspace root (bounded by the
configured startup timeout), and the run's teardown closes it. A server that
fails to start, exits, or breaks the protocol moves to ``failed`` and stays
there: it is never silently restarted, so an answer always comes from the
process whose state the status reports.

The client advertises no ``workspace/applyEdit`` or ``executeCommand`` support
and refuses an edit the server asks to apply. It sends only open/change
notifications and read-labelled requests; that labelling is not proof the
server writes no caches or keeps to the workspace.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import signal
from collections import OrderedDict, deque

from garuda.lsp.config import LanguageServerConfig, locate_executable
from garuda.lsp.positions import path_to_uri, uri_to_path
from garuda.lsp.protocol import (
    REQUEST_CAPABILITIES,
    Diagnostic,
    DiagnosticsReport,
    LanguageServiceError,
    Location,
    Range,
    ServiceState,
    ServiceStatus,
    Symbol,
    UnsupportedRequest,
)

logger = logging.getLogger(__name__)

# One framed message from the server. A larger one fails the server rather than
# being buffered without bound or skipped (skipping would desynchronise ids).
MAX_MESSAGE_BYTES = 8 * 1024 * 1024
# Largest document sent to the server.
MAX_DOCUMENT_BYTES = 2 * 1024 * 1024
MAX_OPEN_DOCUMENTS = 200
MAX_DIAGNOSTIC_DOCUMENTS = 1000
MAX_DIAGNOSTICS_PER_DOCUMENT = 500
MAX_LOCATIONS = 1000
MAX_SYMBOLS = 2000
MAX_HOVER_CHARS = 20_000
STDERR_TAIL_BYTES = 4096
SHUTDOWN_TIMEOUT = 2.0

# Inherited by the server process; anything else (provider keys included) is
# passed only when the trusted configuration names it under ``env``.
_INHERITED_ENV = (
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "LC_ALL", "LC_CTYPE",
    "TMPDIR", "VIRTUAL_ENV", "CONDA_PREFIX", "PYTHONPATH", "NODE_PATH", "GOPATH",
    "GOROOT", "CARGO_HOME", "RUSTUP_HOME", "JAVA_HOME",
)

_METHOD_NOT_FOUND = -32601


def _range(raw) -> Range:
    if not isinstance(raw, dict):
        raise ValueError("range")
    start, end = raw.get("start") or {}, raw.get("end") or {}
    values = (start.get("line"), start.get("character"), end.get("line"), end.get("character"))
    if not all(isinstance(v, int) and not isinstance(v, bool) and v >= 0 for v in values):
        raise ValueError("range")
    return Range(*values)


def _locations(result) -> list[Location]:
    """Location | Location[] | LocationLink[] | null, malformed entries dropped."""
    if result is None:
        return []
    items = result if isinstance(result, list) else [result]
    out: list[Location] = []
    for item in items[:MAX_LOCATIONS]:
        if not isinstance(item, dict):
            continue
        try:
            if "targetUri" in item:
                rng = item.get("targetSelectionRange") or item.get("targetRange")
                out.append(Location(str(item["targetUri"]), _range(rng)))
            else:
                out.append(Location(str(item["uri"]), _range(item.get("range"))))
        except (KeyError, ValueError):
            continue
    return out


def _hover_text(result) -> str | None:
    if not isinstance(result, dict):
        return None
    contents = result.get("contents")
    parts: list[str] = []
    for item in contents if isinstance(contents, list) else [contents]:
        if isinstance(item, str):
            parts.append(item)
        elif isinstance(item, dict) and isinstance(item.get("value"), str):
            parts.append(item["value"])
    text = "\n\n".join(p for p in parts if p.strip())
    return text[:MAX_HOVER_CHARS] or None


def _symbols(result) -> list[Symbol]:
    """DocumentSymbol[] (hierarchical) or SymbolInformation[], flattened in order."""
    out: list[Symbol] = []

    def walk(items, depth: int) -> None:
        for item in items if isinstance(items, list) else []:
            if len(out) >= MAX_SYMBOLS:
                return
            if not isinstance(item, dict) or not isinstance(item.get("name"), str):
                continue
            try:
                if "location" in item:
                    rng = _range((item.get("location") or {}).get("range"))
                else:
                    rng = _range(item.get("selectionRange") or item.get("range"))
            except ValueError:
                continue
            kind = item.get("kind") if isinstance(item.get("kind"), int) else 0
            detail = item.get("detail") or item.get("containerName") or ""
            out.append(Symbol(item["name"], kind, rng, depth, str(detail)[:200]))
            walk(item.get("children"), depth + 1)

    walk(result, 0)
    return out


def _diagnostic(raw) -> Diagnostic | None:
    if not isinstance(raw, dict) or not isinstance(raw.get("message"), str):
        return None
    try:
        rng = _range(raw.get("range"))
    except ValueError:
        return None
    severity = raw.get("severity") if isinstance(raw.get("severity"), int) else None
    code = raw.get("code")
    return Diagnostic(
        rng, raw["message"][:1000], severity, str(raw.get("source") or "")[:80],
        "" if code is None else str(code)[:80],
    )


def _doc_key(path: str) -> str:
    return os.path.realpath(path)


class StdioLanguageServer:
    """One configured language server; implements :class:`LanguageService`."""

    def __init__(self, config: LanguageServerConfig):
        self.config = config
        self.name = config.name
        self.extensions = config.extensions
        self._state = ServiceState.CONFIGURED
        self._detail = ""
        self._root: str | None = None
        self._process: asyncio.subprocess.Process | None = None
        self._tasks: list[asyncio.Task] = []
        self._pending: dict[int, asyncio.Future] = {}
        self._next_id = 1
        self._capabilities: dict = {}
        self._start_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()
        self._doc_lock = asyncio.Lock()
        # key -> (uri, version, text), least recently used first.
        self._docs: OrderedDict[str, tuple[str, int, str]] = OrderedDict()
        # key -> (published version or None, sequence, diagnostics)
        self._published: OrderedDict[str, tuple[int | None, int, tuple[Diagnostic, ...]]] = OrderedDict()
        self._publish_seq = 0
        self._synced_seq: dict[str, int] = {}
        self._published_event = asyncio.Event()
        self._stderr_tail: deque[bytes] = deque()
        self._stderr_size = 0

    # -- status -------------------------------------------------------------

    def status(self) -> ServiceStatus:
        supported: tuple[str, ...] = ()
        unsupported: tuple[str, ...] = ()
        if self._state is ServiceState.READY:
            supported = tuple(k for k, cap in REQUEST_CAPABILITIES.items() if self._capabilities.get(cap))
            unsupported = tuple(k for k in REQUEST_CAPABILITIES if k not in supported)
        return ServiceStatus(
            name=self.name, state=self._state, extensions=self.extensions,
            command=self.config.command_line, detail=self._detail,
            supported=supported, unsupported=unsupported, root=self._root,
        )

    def _fail(self, detail: str) -> None:
        if self._state in (ServiceState.FAILED, ServiceState.CLOSED):
            return
        tail = b"".join(self._stderr_tail).decode(errors="replace").strip()
        self._detail = detail + (f"; stderr: {tail[-500:]}" if tail else "")
        self._state = ServiceState.FAILED
        logger.warning("Language server %s failed: %s", self.name, self._detail)
        self._reject_pending(f"language server {self.name} failed: {detail}")
        self._kill()

    def _reject_pending(self, message: str) -> None:
        pending, self._pending = self._pending, {}
        for future in pending.values():
            if not future.done():
                future.set_exception(LanguageServiceError(message))

    def _kill(self) -> None:
        process = self._process
        if process is None or process.returncode is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            with contextlib.suppress(ProcessLookupError):
                process.kill()

    def _unavailable(self) -> LanguageServiceError:
        if self._state is ServiceState.FAILED:
            return LanguageServiceError(
                f"language server {self.name} is in failed state ({self._detail}); "
                "it is not restarted during this run")
        return LanguageServiceError(f"language server {self.name} is {self._state.value}")

    # -- lifecycle ----------------------------------------------------------

    async def _ensure_started(self, root: str) -> None:
        async with self._start_lock:
            if self._state is ServiceState.READY:
                if root != self._root:
                    raise LanguageServiceError(
                        f"language server {self.name} is bound to {self._root}, not {root}")
                return
            if self._state is not ServiceState.CONFIGURED:
                raise self._unavailable()
            self._state = ServiceState.STARTING
            self._root = root
            try:
                await asyncio.wait_for(self._launch(root), self.config.startup_timeout)
            except (TimeoutError, asyncio.TimeoutError):
                self._fail(f"did not finish initialize within {self.config.startup_timeout:g}s")
            except LanguageServiceError as exc:
                self._fail(str(exc))
            except BaseException:
                self._fail("startup was cancelled")
                raise
            if self._state is not ServiceState.READY:
                raise self._unavailable()

    async def _launch(self, root: str) -> None:
        executable = locate_executable(self.config)
        if executable is None:
            raise LanguageServiceError(f"executable not found: {self.config.command}")
        env = {k: os.environ[k] for k in _INHERITED_ENV if k in os.environ}
        env.update(self.config.env)
        try:
            self._process = await asyncio.create_subprocess_exec(
                executable, *self.config.args,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, cwd=root, env=env,
                start_new_session=True,  # own process group, so close/kill reaps children
            )
        except OSError as exc:
            raise LanguageServiceError(f"cannot start {self.config.command}: {exc}") from exc
        self._tasks = [
            asyncio.create_task(self._read_loop()),
            asyncio.create_task(self._drain_stderr()),
        ]
        root_uri = path_to_uri(root)
        result = await self._request_raw("initialize", {
            "processId": os.getpid(),
            "clientInfo": {"name": "garuda"},
            "rootUri": root_uri,
            "workspaceFolders": [{"uri": root_uri, "name": os.path.basename(root) or root}],
            "capabilities": {
                "general": {"positionEncodings": ["utf-16"]},
                "textDocument": {
                    "synchronization": {"didSave": False, "willSave": False},
                    "definition": {"linkSupport": True},
                    "references": {},
                    "hover": {"contentFormat": ["plaintext", "markdown"]},
                    "documentSymbol": {"hierarchicalDocumentSymbolSupport": True},
                    "publishDiagnostics": {"versionSupport": True},
                },
                "workspace": {"workspaceFolders": True, "configuration": True},
            },
        }, timeout=None)
        if not isinstance(result, dict) or not isinstance(result.get("capabilities"), dict):
            raise LanguageServiceError("initialize returned no capabilities")
        capabilities = result["capabilities"]
        encoding = capabilities.get("positionEncoding", "utf-16")
        if encoding != "utf-16":
            raise LanguageServiceError(f"server chose unsupported position encoding {encoding!r}")
        self._capabilities = capabilities
        await self._notify("initialized", {})
        self._state = ServiceState.READY

    async def close(self) -> None:
        if self._state is ServiceState.CLOSED:
            return
        was_ready = self._state is ServiceState.READY
        process = self._process
        if was_ready and process is not None and process.returncode is None:
            with contextlib.suppress(Exception):
                await self._request_raw("shutdown", None, timeout=SHUTDOWN_TIMEOUT)
                await self._notify("exit", None)
        self._state = ServiceState.CLOSED
        self._reject_pending(f"language server {self.name} closed")
        if process is not None:
            if process.stdin is not None:
                with contextlib.suppress(Exception):
                    process.stdin.close()
            try:
                await asyncio.wait_for(process.wait(), SHUTDOWN_TIMEOUT)
            except (TimeoutError, asyncio.TimeoutError):
                pass
            self._kill()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(process.wait(), SHUTDOWN_TIMEOUT)
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []

    # -- transport ----------------------------------------------------------

    async def _drain_stderr(self) -> None:
        assert self._process is not None and self._process.stderr is not None
        while chunk := await self._process.stderr.read(4096):
            self._stderr_tail.append(chunk)
            self._stderr_size += len(chunk)
            while self._stderr_size > STDERR_TAIL_BYTES and len(self._stderr_tail) > 1:
                self._stderr_size -= len(self._stderr_tail.popleft())

    async def _read_message(self) -> dict | None:
        stdout = self._process.stdout
        length = None
        while True:
            line = await stdout.readline()
            if not line:
                return None
            if line in (b"\r\n", b"\n"):
                break
            name, _, value = line.decode("ascii", errors="replace").partition(":")
            if name.strip().lower() == "content-length":
                length = int(value.strip())
        if length is None or length < 0:
            raise LanguageServiceError("message without a valid Content-Length")
        if length > MAX_MESSAGE_BYTES:
            raise LanguageServiceError(
                f"message of {length} bytes exceeds the {MAX_MESSAGE_BYTES}-byte limit")
        body = await stdout.readexactly(length)
        message = json.loads(body.decode("utf-8"))
        if not isinstance(message, dict):
            raise LanguageServiceError("message is not a JSON object")
        return message

    async def _read_loop(self) -> None:
        try:
            while True:
                message = await self._read_message()
                if message is None:
                    break
                await self._dispatch(message)
        except asyncio.CancelledError:
            raise
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, ValueError,
                LanguageServiceError) as exc:
            self._fail(f"protocol error: {exc}")
            return
        except Exception as exc:  # pragma: no cover - defensive
            self._fail(f"reader error: {type(exc).__name__}: {exc}")
            return
        if self._state is not ServiceState.CLOSED:
            code = None
            if self._process is not None:
                with contextlib.suppress(Exception):
                    code = await asyncio.wait_for(self._process.wait(), SHUTDOWN_TIMEOUT)
            self._fail(f"server exited (exit code {code})")

    async def _dispatch(self, message: dict) -> None:
        method = message.get("method")
        if method is None:
            future = self._pending.pop(message.get("id"), None)
            if future is None or future.done():
                return
            error = message.get("error")
            if error is not None:
                detail = error.get("message") if isinstance(error, dict) else error
                code = error.get("code") if isinstance(error, dict) else None
                future.set_exception(LanguageServiceError(f"server error {code}: {detail}"))
            else:
                future.set_result(message.get("result"))
            return
        if "id" in message:
            await self._answer(message["id"], method, message.get("params"))
        elif method == "textDocument/publishDiagnostics":
            self._record_diagnostics(message.get("params"))

    async def _answer(self, request_id, method: str, params) -> None:
        """Answer a server-to-client request without acting on the workspace."""
        if method == "workspace/configuration":
            items = params.get("items") if isinstance(params, dict) else None
            result = [None] * (len(items) if isinstance(items, list) else 0)
        elif method == "workspace/applyEdit":
            result = {"applied": False, "failureReason": "Garuda's language client is read-only"}
        elif method == "workspace/workspaceFolders":
            root = self._root or ""
            result = [{"uri": path_to_uri(root), "name": os.path.basename(root) or root}]
        elif method in ("client/registerCapability", "client/unregisterCapability",
                        "window/workDoneProgress/create", "window/showMessageRequest"):
            result = None
        else:
            await self._send({"jsonrpc": "2.0", "id": request_id, "error": {
                "code": _METHOD_NOT_FOUND, "message": f"unsupported by this client: {method}"}})
            return
        await self._send({"jsonrpc": "2.0", "id": request_id, "result": result})

    def _record_diagnostics(self, params) -> None:
        if not isinstance(params, dict) or not isinstance(params.get("diagnostics"), list):
            return
        path = uri_to_path(str(params.get("uri", "")))
        if path is None:
            return
        version = params.get("version")
        if isinstance(version, bool) or not isinstance(version, int):
            version = None
        items = [_diagnostic(d) for d in params["diagnostics"][:MAX_DIAGNOSTICS_PER_DOCUMENT]]
        self._publish_seq += 1
        key = _doc_key(path)
        self._published[key] = (version, self._publish_seq, tuple(d for d in items if d))
        self._published.move_to_end(key)
        while len(self._published) > MAX_DIAGNOSTIC_DOCUMENTS:
            self._published.popitem(last=False)
        event, self._published_event = self._published_event, asyncio.Event()
        event.set()

    async def _send(self, message: dict) -> None:
        process = self._process
        if process is None or process.stdin is None:
            raise LanguageServiceError(f"language server {self.name} is not running")
        body = json.dumps(message, separators=(",", ":")).encode()
        async with self._write_lock:
            try:
                process.stdin.write(b"Content-Length: %d\r\n\r\n" % len(body) + body)
                await process.stdin.drain()
            except (BrokenPipeError, ConnectionResetError) as exc:
                self._fail(f"server stopped reading its input ({type(exc).__name__})")
                raise self._unavailable() from exc

    async def _notify(self, method: str, params) -> None:
        await self._send({"jsonrpc": "2.0", "method": method, "params": params})

    async def _request_raw(self, method: str, params, *, timeout: float | None):
        request_id = self._next_id
        self._next_id += 1
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            await self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
            if timeout is None:
                return await future
            return await asyncio.wait_for(future, timeout)
        except (TimeoutError, asyncio.TimeoutError):
            await self._cancel_remote(request_id)
            raise LanguageServiceError(
                f"{method} timed out after {timeout:g}s; the request was cancelled") from None
        except asyncio.CancelledError:
            await self._cancel_remote(request_id)
            raise
        finally:
            self._pending.pop(request_id, None)

    async def _cancel_remote(self, request_id: int) -> None:
        if self._state is ServiceState.READY:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(
                    self._notify("$/cancelRequest", {"id": request_id}), SHUTDOWN_TIMEOUT)

    async def _request(self, kind: str, method: str, params):
        if self._state is not ServiceState.READY:
            raise self._unavailable()
        capability = REQUEST_CAPABILITIES.get(kind)
        if capability and not self._capabilities.get(capability):
            raise UnsupportedRequest(
                f"language server {self.name} does not support {method} "
                f"(no {capability} in its capabilities)")
        return await self._request_raw(method, params, timeout=self.config.request_timeout)

    # -- documents ----------------------------------------------------------

    async def sync(self, path: str, text: str, root: str) -> int:
        if len(text.encode("utf-8", errors="surrogatepass")) > MAX_DOCUMENT_BYTES:
            raise LanguageServiceError(
                f"{path} is larger than the {MAX_DOCUMENT_BYTES}-byte language-service limit")
        await self._ensure_started(root)
        async with self._doc_lock:
            key = _doc_key(path)
            current = self._docs.get(key)
            if current is None:
                uri, version = path_to_uri(path), 1
                # A reopened document restarts at version 1; diagnostics kept
                # from before it was closed describe a different version series.
                self._published.pop(key, None)
                self._synced_seq[key] = self._publish_seq
                await self._notify("textDocument/didOpen", {"textDocument": {
                    "uri": uri, "languageId": self.config.language_id,
                    "version": version, "text": text}})
            else:
                uri, version, sent = current
                if sent != text:
                    version += 1
                    self._synced_seq[key] = self._publish_seq
                    await self._notify("textDocument/didChange", {
                        "textDocument": {"uri": uri, "version": version},
                        "contentChanges": [{"text": text}]})
            self._docs[key] = (uri, version, text)
            self._docs.move_to_end(key)
            while len(self._docs) > MAX_OPEN_DOCUMENTS:
                old_key, (old_uri, _, _) = self._docs.popitem(last=False)
                self._synced_seq.pop(old_key, None)
                await self._notify("textDocument/didClose", {"textDocument": {"uri": old_uri}})
            return version

    def _position_params(self, path: str, line: int, character: int) -> dict:
        uri = self._docs[_doc_key(path)][0]
        return {"textDocument": {"uri": uri}, "position": {"line": line, "character": character}}

    async def definition(self, path: str, line: int, character: int) -> list[Location]:
        result = await self._request(
            "definition", "textDocument/definition", self._position_params(path, line, character))
        return _locations(result)

    async def references(
        self, path: str, line: int, character: int, include_declaration: bool
    ) -> list[Location]:
        params = self._position_params(path, line, character)
        params["context"] = {"includeDeclaration": bool(include_declaration)}
        return _locations(await self._request("references", "textDocument/references", params))

    async def hover(self, path: str, line: int, character: int) -> str | None:
        result = await self._request(
            "hover", "textDocument/hover", self._position_params(path, line, character))
        return _hover_text(result)

    async def symbols(self, path: str) -> list[Symbol]:
        uri = self._docs[_doc_key(path)][0]
        result = await self._request(
            "symbols", "textDocument/documentSymbol", {"textDocument": {"uri": uri}})
        return _symbols(result)

    async def diagnostics(self, path: str, wait: float) -> DiagnosticsReport:
        """Diagnostics for the version last synced, waiting up to ``wait`` seconds.

        Waits for a publication for exactly that version, or for any publication
        after the sync when the server does not version them.
        """
        if self._state is not ServiceState.READY:
            raise self._unavailable()
        key = _doc_key(path)
        version = self._docs[key][1]
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.0, wait)
        while True:
            record = self._published.get(key)
            if record is not None:
                published, seq, _ = record
                if published == version:
                    break
                if published is None and seq > self._synced_seq.get(key, 0):
                    break
            remaining = deadline - loop.time()
            if remaining <= 0 or self._state is not ServiceState.READY:
                break
            with contextlib.suppress(TimeoutError, asyncio.TimeoutError):
                await asyncio.wait_for(self._published_event.wait(), remaining)
        if self._state is not ServiceState.READY:
            raise self._unavailable()
        record = self._published.get(key)
        if record is None:
            return DiagnosticsReport(version, None, "none")
        published, seq, items = record
        if published is None:
            # Not bound to a version. Published before the latest sync means it
            # describes an earlier text; after it, currency is still unprovable.
            freshness = "unversioned" if seq > self._synced_seq.get(key, 0) else "stale"
        elif published == version:
            freshness = "current"
        elif published < version:
            freshness = "stale"
        else:
            freshness = "unknown"
        return DiagnosticsReport(version, published, freshness, items)
