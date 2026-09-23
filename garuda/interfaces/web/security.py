"""The dashboard's request gate: Host, Origin, and bearer token.

Separate from the server so it is unit-testable without a socket and reviewable on
its own. Every rule below fails closed, and the reason each exists is stated,
because the threat model here is not obvious: this is a loopback server that in
write mode can execute arbitrary code as the developer.

Why this exists at all instead of reusing ``interfaces/server.py``'s gate: that one
rejects **any** request carrying an ``Origin`` header, which is exactly right for a
JSON-RPC surface only programmatic clients use, and exactly wrong here — a browser
always sends Origin. So this is an allowlist, and it must not be relaxed into
``server.py``.

The layers, outermost first:

**Host** is the load-bearing one, and the only defence against DNS rebinding. A
top-level navigation to ``http://attacker.test:8787/`` whose A record points at
127.0.0.1 sends **no Origin header at all**, so an Origin-only policy lets it
straight through and the page then reads every response same-origin. Checking Host
on every request — GET included — is what closes that, because we know our own port.

**Origin** closes the cross-site form-POST path, which is why it is *required* on
state-changing methods and merely validated when present on reads.

**Token** exists because loopback is not a trust boundary — the same conclusion
``server.py::ensure_secure_config`` already reached, for a strictly smaller surface.
Any local process, any ``npm postinstall``, any page the developer happens to visit
can ``fetch("http://127.0.0.1:8787/api/runs")``. The read surface alone serves
``messages.json`` and raw tool output: source code, ``env`` dumps, keys that appeared
in bash output. It is delivered in the URL *fragment* (never sent to a server, never
in a log or ``Referer``) and returned in a custom header, which cannot be set
cross-origin without a preflight — and this module never answers one.
"""

from __future__ import annotations

import hmac
import os
import secrets

from garuda.interfaces.web.wire import (
    CODE_FORBIDDEN_HOST,
    CODE_FORBIDDEN_ORIGIN,
    CODE_METHOD_NOT_ALLOWED,
    CODE_PAYLOAD_TOO_LARGE,
    CODE_UNAUTHORIZED,
    Request,
    Response,
    error,
)

#: Not 8765: colliding with `garuda serve` would make a stale-port failure look like
#: an authentication failure.
DEFAULT_PORT = 8787
#: A stale dashboard from a previous session is the common case, so walk past a busy
#: port rather than making the first experience "address in use, figure it out".
#: Bounded, so a genuinely wedged machine gets a real error naming --port.
PORT_WALK_LIMIT = 10

TOKEN_HEADER = "x-garuda-token"
TOKEN_ENV_VAR = "GARUDA_WEB_TOKEN"

#: 12 MiB, still well under `serve`'s 32 MiB.
#:
#: It was 1 MiB while the largest legitimate body was a chat message. Grounding an answer in
#: an uploaded PDF made the largest legitimate body a document, base64-encoded — so the cap
#: has to clear `grounding.MAX_SOURCE_BYTES` (8 MiB) plus its 4/3 encoding overhead, or the
#: feature refuses exactly the files it exists for.
#:
#: The number that bounds memory is this times `MAX_CONNECTIONS`, and 32 is deliberately
#: small for that reason. A declared Content-Length over the cap is refused before a byte of
#: it is read, so the ceiling is on what is *accepted*, not on what can be announced.
MAX_REQUEST_BODY_BYTES = 12 * 1024 * 1024
#: Slow-loris protection, matching `server.py`'s REQUEST_READ_TIMEOUT.
REQUEST_TIMEOUT_SECONDS = 30.0
#: Thread-per-connection needs a ceiling, or a hostile local process opens 10k.
MAX_CONNECTIONS = 32

LOOPBACK_HOSTNAMES = ("127.0.0.1", "localhost")
STATE_CHANGING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

#: Applied to every response. The CSP is what forbids inline script and style, which
#: is a constraint the frontend has to respect from its first line: no <script> body,
#: no <style> block, no style="..." attribute. SVG charts therefore use presentation
#: attributes and CSS classes.
SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "font-src 'self'; connect-src 'self'; base-uri 'none'; form-action 'none'; "
        "frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    # Never Access-Control-Allow-Origin. Even if a cross-origin page reaches the
    # socket, the browser discards the body. Answering a preflight is the one way to
    # hand that away, so OPTIONS is refused below rather than handled.
}

#: Paths served without a token: they carry no user data, and the shell has to load
#: before any JS exists to send a header.
PUBLIC_PATH_PREFIXES = ("/static/",)
PUBLIC_PATHS = frozenset({"/", "/index.html", "/favicon.ico"})


def generate_token() -> str:
    """The session's token: ``GARUDA_WEB_TOKEN`` if set, else a fresh random one."""
    return os.environ.get(TOKEN_ENV_VAR) or secrets.token_urlsafe(32)


def _is_public(path: str) -> bool:
    return path in PUBLIC_PATHS or path.startswith(PUBLIC_PATH_PREFIXES)


def allowed_origins(port: int) -> tuple[str, ...]:
    return tuple(f"http://{host}:{port}" for host in LOOPBACK_HOSTNAMES)


def allowed_hosts(port: int) -> tuple[str, ...]:
    return tuple(f"{host}:{port}" for host in LOOPBACK_HOSTNAMES)


def check_request(request: Request, *, port: int, token: str) -> Response | None:
    """``None`` when the request may proceed, else the refusal to send instead.

    Order matters: Host before Origin before token, so a rebinding attempt is
    refused without the response ever depending on whether it guessed the token.
    """
    if request.method == "OPTIONS":
        # No CORS preflight is ever answered — see SECURITY_HEADERS.
        return error(CODE_METHOD_NOT_ALLOWED, "OPTIONS is not supported.", status=405)

    if len(request.body) > MAX_REQUEST_BODY_BYTES:
        return error(CODE_PAYLOAD_TOO_LARGE, "Request body is too large.", status=413)

    host = request.headers.get("host", "")
    if host not in allowed_hosts(port):
        # Absent or unrecognised, both refused. A bare "127.0.0.1" with no port is
        # refused too: we know which port we bound.
        return error(
            CODE_FORBIDDEN_HOST,
            f"Host {host!r} is not permitted; reach this dashboard as "
            f"http://127.0.0.1:{port}/.",
            status=403,
        )

    origin = request.headers.get("origin")
    permitted = allowed_origins(port)
    if origin is not None:
        # `Origin: null` (a sandboxed iframe, a data: URL) is not an allowlist entry.
        if origin not in permitted:
            return error(
                CODE_FORBIDDEN_ORIGIN, f"Origin {origin!r} is not permitted.", status=403
            )
    elif request.method in STATE_CHANGING_METHODS:
        # Browsers always send Origin on fetch and on cross-site form posts, so
        # requiring it here closes the <form> CSRF path. Reads are exempt because a
        # plain navigation or curl legitimately sends none, and Host already screened.
        return error(
            CODE_FORBIDDEN_ORIGIN,
            "A state-changing request must carry an Origin header.",
            status=403,
        )

    if _is_public(request.path):
        return None

    provided = request.headers.get(TOKEN_HEADER, "")
    # Constant-time, so the token cannot be recovered by timing.
    if not token or not hmac.compare_digest(provided, token):
        return error(
            CODE_UNAUTHORIZED,
            "Missing or invalid dashboard token. Relaunch `garuda web` and open the "
            "URL it prints.",
            status=401,
        )
    return None
