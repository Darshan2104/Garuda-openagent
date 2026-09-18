"""Dashboard HTTP gate: Host allowlist, Origin allowlist, and token auth.

Pure `check_request` / `dispatch` calls, no sockets — the same shape
`JsonRpcServer.handle()` is tested in.

The threat model, stated once because every assertion here follows from it: this is a
loopback server that in write mode executes code as the developer, and loopback is not
a trust boundary (`server.py::ensure_secure_config` already reached that conclusion for
a smaller surface). The Host check is the load-bearing one — a top-level navigation to
a hostname that resolves to 127.0.0.1 sends no Origin at all, so an Origin-only policy
would let a rebinding attack read every response same-origin.
"""

import pytest

from garuda.core.sessions import SessionStore
from garuda.interfaces.web.routes import DashboardContext, dispatch
from garuda.interfaces.web.security import (
    MAX_REQUEST_BODY_BYTES,
    SECURITY_HEADERS,
    TOKEN_HEADER,
    check_request,
)
from garuda.interfaces.web.wire import Request

PORT = 8787
TOKEN = "test-token-value"


@pytest.fixture
def ctx(tmp_path):
    return DashboardContext(port=PORT, token=TOKEN, store=SessionStore(root=tmp_path))


def _request(method="GET", path="/api/health", *, host=f"127.0.0.1:{PORT}",
             origin=None, token=TOKEN, body=b""):
    headers = {}
    if host is not None:
        headers["host"] = host
    if origin is not None:
        headers["origin"] = origin
    if token is not None:
        headers[TOKEN_HEADER] = token
    return Request(method=method, path=path, headers=headers, body=body)


def _refuse(**kwargs):
    return check_request(_request(**kwargs), port=PORT, token=TOKEN)


def _code(response):
    import json

    return json.loads(response.body)["error"]["code"]


# --- Host: the DNS-rebinding defence ----------------------------------------


@pytest.mark.parametrize("host", [f"127.0.0.1:{PORT}", f"localhost:{PORT}"])
def test_loopback_hosts_are_permitted(host):
    assert _refuse(host=host) is None


@pytest.mark.parametrize(
    "host",
    [
        "evil.test:8787",  # the rebinding case: resolves to 127.0.0.1, sends no Origin
        "127.0.0.1:9999",  # right address, wrong port — not the server we bound
        "127.0.0.1",  # no port at all; we know ours
        "localhost",
        "",
    ],
)
def test_other_hosts_are_refused(host):
    response = _refuse(host=host)
    assert response is not None and response.status == 403
    assert _code(response) == "forbidden_host"


def test_an_absent_host_header_is_refused():
    """Fail closed: a client that sends no Host gets no answer."""
    response = check_request(_request(host=None), port=PORT, token=TOKEN)
    assert response is not None and response.status == 403


def test_the_host_check_runs_before_the_token_check():
    """A rebinding attempt must be refused without the answer depending on whether
    it guessed the token."""
    response = _refuse(host="evil.test:8787", token="wrong")
    assert _code(response) == "forbidden_host"


# --- Origin ------------------------------------------------------------------


def test_an_absent_origin_is_fine_on_a_read():
    """A plain navigation or curl sends none, and Host already screened it."""
    assert _refuse(method="GET", origin=None) is None


@pytest.mark.parametrize("origin", [f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}"])
def test_matching_origins_are_permitted(origin):
    assert _refuse(origin=origin) is None


@pytest.mark.parametrize(
    "origin",
    [
        "http://evil.test:8787",
        f"https://127.0.0.1:{PORT}",  # wrong scheme
        f"http://127.0.0.1:{PORT + 1}",  # wrong port
        "null",  # a sandboxed iframe or data: URL is not an allowlist entry
    ],
)
def test_other_origins_are_refused(origin):
    response = _refuse(origin=origin)
    assert response is not None and response.status == 403
    assert _code(response) == "forbidden_origin"


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_a_state_changing_request_must_carry_an_origin(method):
    """Closes the cross-site <form> POST path, which needs no Origin to be sent."""
    response = _refuse(method=method, origin=None)
    assert response is not None and response.status == 403
    assert _code(response) == "forbidden_origin"


# --- token -------------------------------------------------------------------


def test_a_missing_or_wrong_token_is_unauthorized():
    for provided in (None, "", "not-the-token"):
        response = _refuse(token=provided)
        assert response is not None and response.status == 401, provided
        assert _code(response) == "unauthorized"


def test_the_shell_and_its_assets_are_token_exempt():
    """The page has to load before any JS exists to send a header. They carry no
    user data; Host and Origin still apply."""
    for path in ("/", "/index.html", "/favicon.ico", "/static/app.css"):
        assert _refuse(path=path, token=None) is None, path


def test_an_api_path_is_never_token_exempt():
    assert _refuse(path="/api/runs", token=None) is not None
    # And a path that merely *starts* like a public one is not exempt either.
    assert _refuse(path="/static-secret/x", token=None) is not None


def test_an_empty_server_token_refuses_everything():
    """A server with no token must not accept an empty client token as a match."""
    response = check_request(_request(token=""), port=PORT, token="")
    assert response is not None and response.status == 401


# --- method and size --------------------------------------------------------


def test_options_is_refused_rather_than_answered():
    """Answering a CORS preflight is the one way to hand a cross-origin page a
    readable response, so the code path does not exist."""
    response = _refuse(method="OPTIONS")
    assert response is not None and response.status == 405


def test_an_oversized_body_is_refused():
    response = _refuse(method="POST", origin=f"http://127.0.0.1:{PORT}",
                       body=b"x" * (MAX_REQUEST_BODY_BYTES + 1))
    assert response is not None and response.status == 413


# --- response headers --------------------------------------------------------


def test_no_cors_header_is_ever_emitted(ctx):
    """Asserted over the whole route table, not one route."""
    paths = ["/api/health", "/api/config", "/api/runs", "/api/agents", "/api/nope"]
    for path in paths:
        response = dispatch(_request(path=path), ctx)
        for name in list(response.headers) + list(SECURITY_HEADERS):
            assert not name.lower().startswith("access-control-"), (path, name)


def test_the_security_headers_forbid_inline_script_and_style():
    """The frontend depends on this being true: no inline <script>, no <style>
    block, no style="..." attribute anywhere in the static files."""
    csp = SECURITY_HEADERS["Content-Security-Policy"]
    assert "unsafe-inline" not in csp
    assert "unsafe-eval" not in csp
    assert "default-src 'none'" in csp
    assert "frame-ancestors 'none'" in csp
    assert SECURITY_HEADERS["X-Content-Type-Options"] == "nosniff"
    assert SECURITY_HEADERS["Referrer-Policy"] == "no-referrer"
    assert SECURITY_HEADERS["X-Frame-Options"] == "DENY"


# --- write mode --------------------------------------------------------------


def test_write_routes_are_unavailable_without_allow_run(ctx):
    from garuda.interfaces.web.routes import requires_write

    assert ctx.allow_run is False
    refusal = requires_write(ctx)
    assert refusal is not None and refusal.status == 503
    assert _code(refusal) == "read_only"

    ctx.allow_run = True
    assert requires_write(ctx) is None


def test_capabilities_track_allow_run(ctx):
    """The chat capability needs the flag *and* an attached agent loop.

    Both halves, because either alone is a lie: without the flag the routes 503, and
    without a loop there is nothing for an HTTP thread to marshal a turn onto. The UI
    hides its controls off this rather than offering buttons that fail."""
    assert ctx.capabilities["chat"] is False
    ctx.allow_run = True
    assert ctx.capabilities["chat"] is False, "the flag alone is not enough"
    ctx.live = object()
    assert ctx.capabilities["chat"] is True
