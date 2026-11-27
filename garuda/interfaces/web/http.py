"""The dashboard's HTTP server: stdlib, threaded, loopback-only.

``ThreadingHTTPServer`` rather than the ``asyncio.start_server`` style
``interfaces/server.py`` uses, for three reasons:

1. **The read path is blocking file I/O.** Parsing a large ``events.jsonl`` inside a
   pure-asyncio server stalls the event loop — which, in write mode, is the loop
   running the agent whose trajectory is being read. Every read would need
   ``to_thread`` anyway; this way the threads are the transport and are correct by
   construction.
2. **Write mode still needs the loop.** ``JobManager.submit`` calls
   ``asyncio.create_task``, so it must be driven from loop context. A handler thread
   reaches it with one ``run_coroutine_threadsafe`` call.
3. **``server.py``'s loop is not reusable.** It is POST-only with a hand-rolled read
   loop, no request line parsing, no routing, no content types. Matching its style
   means writing ~150 lines of HTTP the stdlib already ships.

Zero new dependencies either way, so the ``server`` extra stays empty.
"""

from __future__ import annotations

import asyncio
import logging
import re
import threading
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from garuda.interfaces.web.routes import CODE_INTERNAL, DashboardContext, dispatch, sse_error_frame
from garuda.interfaces.web.security import (
    MAX_CONNECTIONS,
    MAX_REQUEST_BODY_BYTES,
    PORT_WALK_LIMIT,
    REQUEST_TIMEOUT_SECONDS,
    SECURITY_HEADERS,
    check_request,
)
from garuda.interfaces.web.wire import (
    CODE_INVALID_REQUEST,
    CODE_METHOD_NOT_ALLOWED,
    CODE_PAYLOAD_TOO_LARGE,
    CODE_UNAVAILABLE,
    Request,
    Response,
    error,
)

logger = logging.getLogger(__name__)

#: The bind address is not configurable. `garuda web` can execute code in write mode,
#: and there is no legitimate remote use that `ssh -L 8787:127.0.0.1:8787` does not
#: cover better. No flag means no misconfiguration.
BIND_HOST = "127.0.0.1"

STATIC_DIR = Path(__file__).parent / "static"

#: An explicit map, not `mimetypes.guess_type`, whose answer comes from the OS
#: registry and returns text/plain for .js on some Windows installs — which silently
#: breaks the whole page.
CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".woff2": "font/woff2",
    ".png": "image/png",
    ".ico": "image/x-icon",
}

#: No slash is expressible, so traversal cannot be spelled in the first place. The
#: resolved-prefix check below then catches a symlink planted in the install tree.
_SAFE_ASSET = re.compile(r"^[A-Za-z0-9._-]+$")


def serve_static(name: str, static_dir: Path) -> Response:
    """One asset from the static directory, or a refusal."""
    if not _SAFE_ASSET.match(name):
        return error("invalid_request", "Bad asset name.", status=400)
    candidate = (static_dir / name).resolve()
    try:
        inside = candidate.is_relative_to(static_dir.resolve())
    except OSError:  # pragma: no cover - a resolve failure on a hostile path
        inside = False
    if not inside or not candidate.is_file():
        return error("not_found", f"No asset {name!r}.", status=404)
    return Response(
        body=candidate.read_bytes(),
        content_type=CONTENT_TYPES.get(candidate.suffix, "application/octet-stream"),
        # no-store, so `pip install -U garuda` can never serve a stale bundle from a
        # cache the user cannot see.
        headers={"Cache-Control": "no-store"},
    )


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    timeout = REQUEST_TIMEOUT_SECONDS
    ctx: DashboardContext  # set on the subclass built in _build_handler

    # Keeps the terminal quiet; a dashboard poll every 750ms would otherwise bury
    # whatever the user is actually watching.
    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        logger.debug("dashboard %s", fmt % args)

    def send_error(self, code: int, message: str | None = None, explain: str | None = None) -> None:
        """The stdlib's own refusals — a malformed request line, an unsupported method, an
        oversized header — in the dashboard's JSON envelope with its security headers,
        instead of ``BaseHTTPRequestHandler``'s HTML page without them."""
        short = self.responses.get(code, ("Error",))[0]
        name = {400: CODE_INVALID_REQUEST, 413: CODE_PAYLOAD_TOO_LARGE,
                414: CODE_PAYLOAD_TOO_LARGE, 431: CODE_PAYLOAD_TOO_LARGE,
                501: CODE_METHOD_NOT_ALLOWED}.get(code, CODE_INVALID_REQUEST)
        self.close_connection = True
        self._send(error(name, message or short, status=code),
                   include_body=self.command != "HEAD" and code >= 200 and code not in (204, 304))

    def _read(self) -> Request | None:
        parts = urlsplit(self.path)
        try:
            lengths = self.headers.get_all("Content-Length", ["0"])
            if ("Transfer-Encoding" in self.headers or len(set(lengths)) != 1
                    or re.fullmatch(r"[0-9]+", lengths[0]) is None):
                raise ValueError("ambiguous or invalid body framing")
            length = int(lengths[0])
        except ValueError:
            self.close_connection = True
            self._send(error("invalid_request", "Invalid Content-Length.", status=400))
            return None
        if length > MAX_REQUEST_BODY_BYTES:
            # Refused on the declared length, before reading, so a large
            # Content-Length cannot be used to exhaust memory.
            self.close_connection = True
            self._send(error("payload_too_large", "Request body is too large.", status=413))
            return None
        request = Request(
            method=self.command,
            path=parts.path,
            query=parse_qs(parts.query),
            headers={key.lower(): value for key, value in self.headers.items()},
        )
        refusal = check_request(request, port=self.ctx.port, token=self.ctx.token)
        if refusal is not None:
            # An unread body cannot become a second request on a persistent socket.
            self.close_connection = True
            self._send(refusal)
            return None
        return replace(request, body=self.rfile.read(length) if length else b"")

    def _send_stream(self, response: Response) -> None:
        self.send_response(response.status)
        self.send_header("Content-Type", response.content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")
        for key, value in SECURITY_HEADERS.items():
            self.send_header(key, value)
        for key, value in response.headers.items():
            self.send_header(key, value)
        self.end_headers()
        self.close_connection = True
        chunks = iter(response.stream)
        try:
            while True:
                try:
                    chunk = next(chunks)
                except StopIteration:
                    break
                except Exception:
                    # Producing the next frame failed (not the socket): log it, tell an
                    # event-stream client in its own framing, and end the response
                    # cleanly instead of dropping the connection mid-stream.
                    logger.exception("dashboard stream for %s failed", self.path)
                    if response.content_type.startswith("text/event-stream"):
                        self.wfile.write(sse_error_frame("The live stream failed."))
                        self.wfile.flush()
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
        except OSError:  # BrokenPipeError, ConnectionResetError: the client went away
            logger.debug("dashboard stream client disconnected")
        finally:
            close = getattr(response.stream, "close", None)
            if close:
                close()

    def _send(self, response: Response, *, include_body: bool = True) -> None:
        if response.stream is not None:
            if include_body:
                self._send_stream(response)
            return
        self.send_response(response.status)
        self.send_header("Content-Type", response.content_type)
        self.send_header("Content-Length", str(len(response.body)))
        for key, value in SECURITY_HEADERS.items():
            self.send_header(key, value)
        for key, value in response.headers.items():
            self.send_header(key, value)
        self.end_headers()
        if include_body and response.body:
            try:
                self.wfile.write(response.body)
            except OSError:  # BrokenPipeError, ConnectionResetError, a write timeout
                # The browser navigated away mid-response. Not an error.
                logger.debug("dashboard client disconnected during write")

    def _handle(self, *, include_body: bool = True) -> None:
        request = self._read()
        if request is None:
            return
        try:
            response = _route(request, self.ctx)
        except Exception:
            # `dispatch` already contains route failures; this is the static path and
            # anything outside a handler. The client still gets the JSON envelope and the
            # security headers, never the stdlib's HTML error page or a dropped socket.
            logger.exception("dashboard request %s %s failed", request.method, request.path)
            response = error(CODE_INTERNAL, "The dashboard failed to handle this request; "
                             "the server log has the details.", status=500)
        self._send(response, include_body=include_body)

    def do_GET(self) -> None:  # noqa: N802
        self._handle()

    def do_HEAD(self) -> None:  # noqa: N802
        self._handle(include_body=False)

    def do_POST(self) -> None:  # noqa: N802
        self._handle()

    def do_DELETE(self) -> None:  # noqa: N802
        self._handle()

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._handle()


def _route(request: Request, ctx: DashboardContext) -> Response:
    """Static assets first, then the JSON API.

    Static is handled here rather than in ``routes.py`` so the traversal root is
    unambiguously this package's own directory and no route ever sees a path.
    """
    static_dir = ctx.static_dir or STATIC_DIR
    if request.path in ("/", "/index.html"):
        refusal = _gate_only(request, ctx)
        return refusal or serve_static("index.html", static_dir)
    if request.path == "/favicon.ico":
        refusal = _gate_only(request, ctx)
        return refusal or Response(status=204, body=b"", content_type="image/x-icon")
    if request.path.startswith("/static/"):
        refusal = _gate_only(request, ctx)
        return refusal or serve_static(request.path[len("/static/"):], static_dir)
    return dispatch(request, ctx)


def _gate_only(request: Request, ctx: DashboardContext) -> Response | None:
    """Run the security gate for a non-API path.

    The shell and its assets are token-exempt (the page has to load before any JS
    exists to send a header), but Host and Origin still apply — those are what stop a
    rebound hostname from loading the app at all.
    """
    return check_request(request, port=ctx.port, token=ctx.token)


#: Seconds the accept loop may spend writing a refusal; it must not stall on one client.
REFUSAL_WRITE_TIMEOUT = 1.0


def refusal_bytes(response: Response) -> bytes:
    """A complete ``Connection: close`` response, written without a handler thread.

    Used where there is no handler to send through — the connection limit refuses before
    a thread exists — so it carries the same security headers every handler response does.
    """
    reason = {503: "Service Unavailable"}.get(response.status, "Error")
    headers = {
        "Content-Type": response.content_type,
        "Content-Length": str(len(response.body)),
        "Connection": "close",
        **SECURITY_HEADERS,
        **response.headers,
    }
    head = f"HTTP/1.1 {response.status} {reason}\r\n" + "".join(
        f"{key}: {value}\r\n" for key, value in headers.items()) + "\r\n"
    return head.encode("latin-1") + response.body


def _too_many_connections() -> bytes:
    response = error(CODE_UNAVAILABLE,
                     "too many open dashboard connections; close some tabs", status=503)
    response.headers["Retry-After"] = "2"
    return refusal_bytes(response)


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, handler, bind_and_activate=True):
        self._slots = threading.BoundedSemaphore(MAX_CONNECTIONS)
        super().__init__(address, handler, bind_and_activate=bind_and_activate)

    def process_request(self, request, client_address):  # type: ignore[override]
        if not self._slots.acquire(blocking=False):
            logger.warning("dashboard refused a connection: %d already in flight", MAX_CONNECTIONS)
            try:
                # This runs on the accept loop: a client that never reads must not stall it.
                settimeout = getattr(request, "settimeout", None)
                if settimeout is not None:
                    settimeout(REFUSAL_WRITE_TIMEOUT)
                request.sendall(_too_many_connections())
            except OSError:
                pass
            super().shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address):  # type: ignore[override]
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()


def bind(ctx: DashboardContext, port: int) -> tuple[_Server, int]:
    """Bind loopback, walking up past a busy port.

    Returns the server and the port actually bound, which is what the printed URL
    must carry — printing the requested port after walking is how a user ends up
    typing a URL that 403s on the Host check.
    """
    handler = type("DashboardHandler", (_Handler,), {"ctx": ctx})
    last: OSError | None = None
    for candidate in range(port, port + PORT_WALK_LIMIT):
        try:
            server = _Server((BIND_HOST, candidate), handler)
        except OSError as exc:
            last = exc
            continue
        ctx.port = candidate  # the gate compares against the bound port, not the ask
        return server, candidate
    raise OSError(
        f"No free port in {port}-{port + PORT_WALK_LIMIT - 1}; pass --port to choose another."
    ) from last


async def serve(ctx: DashboardContext, port: int) -> None:
    """Run until cancelled, keeping the asyncio loop free for write mode."""
    server, bound = bind(ctx, port)
    logger.info("dashboard listening on http://%s:%d", BIND_HOST, bound)
    try:
        await asyncio.to_thread(server.serve_forever)
    finally:
        server.shutdown()
        server.server_close()
