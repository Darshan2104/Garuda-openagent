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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from garuda.interfaces.web.routes import DashboardContext, dispatch
from garuda.interfaces.web.security import (
    MAX_CONNECTIONS,
    MAX_REQUEST_BODY_BYTES,
    PORT_WALK_LIMIT,
    REQUEST_TIMEOUT_SECONDS,
    SECURITY_HEADERS,
    check_request,
)
from garuda.interfaces.web.wire import Request, Response, error

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

    def _read(self) -> Request | None:
        parts = urlsplit(self.path)
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length > MAX_REQUEST_BODY_BYTES:
            # Refused on the declared length, before reading, so a large
            # Content-Length cannot be used to exhaust memory.
            self._send(error("payload_too_large", "Request body is too large.", status=413))
            return None
        body = self.rfile.read(length) if length else b""
        return Request(
            method=self.command,
            path=parts.path,
            query=parse_qs(parts.query),
            headers={key.lower(): value for key, value in self.headers.items()},
            body=body,
        )

    def _send(self, response: Response, *, include_body: bool = True) -> None:
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
            except (BrokenPipeError, ConnectionResetError):
                # The browser navigated away mid-response. Not an error.
                logger.debug("dashboard client disconnected during write")

    def _handle(self, *, include_body: bool = True) -> None:
        request = self._read()
        if request is None:
            return
        response = _route(request, self.ctx)
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


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    #: Thread-per-connection with a ceiling: a hostile local process opening 10k
    #: sockets gets 503s rather than a fork bomb.
    _slots = threading.BoundedSemaphore(MAX_CONNECTIONS)

    def process_request(self, request, client_address):  # type: ignore[override]
        if not self._slots.acquire(blocking=False):
            logger.warning("dashboard refused a connection: %d already in flight", MAX_CONNECTIONS)
            try:
                request.sendall(b"HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\n\r\n")
            except OSError:
                pass
            self.shutdown_request(request)
            return
        super().process_request(request, client_address)

    def shutdown_request(self, request):  # type: ignore[override]
        try:
            super().shutdown_request(request)
        finally:
            try:
                self._slots.release()
            except ValueError:  # pragma: no cover - released more than acquired
                pass


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
