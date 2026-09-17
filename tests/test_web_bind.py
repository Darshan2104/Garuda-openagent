"""The dashboard binds loopback only, walks past a busy port, and prints a usable URL.

Three distinct bugs guarded here. Binding anything but loopback would expose a
surface that in write mode executes code — so the configuration cannot express it.
Printing the *requested* port after walking gives the user a URL that 403s on the
Host check. And printing the token without flushing strands it in a block buffer when
stdout is not a tty, which is the failure `server.py:88-94` documents at length and
this process has exactly the same shape: it prints once, then blocks in serve_forever.
"""

import contextlib
import dataclasses
import io
import socket

import pytest

from garuda.core.sessions import SessionStore
from garuda.interfaces.web import DashboardConfig, build_context, launch_url
from garuda.interfaces.web.http import BIND_HOST, bind
from garuda.interfaces.web.routes import DashboardContext
from garuda.interfaces.web.security import DEFAULT_PORT, PORT_WALK_LIMIT, TOKEN_ENV_VAR


def _ctx(tmp_path, port=0):
    return DashboardContext(port=port, token="t", store=SessionStore(root=tmp_path))


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind((BIND_HOST, 0))
        return probe.getsockname()[1]


def test_the_config_cannot_express_a_non_loopback_bind():
    """No --host flag exists, so there is no way to misconfigure this."""
    fields = {field.name for field in dataclasses.fields(DashboardConfig)}
    assert "host" not in fields
    assert BIND_HOST == "127.0.0.1"


def test_the_bound_socket_is_loopback(tmp_path):
    ctx = _ctx(tmp_path)
    server, port = bind(ctx, _free_port())
    try:
        assert server.socket.getsockname()[0] == "127.0.0.1"
        assert port == ctx.port
    finally:
        server.server_close()


def test_binding_walks_past_a_busy_port(tmp_path):
    first = _free_port()
    with socket.socket() as occupied:
        occupied.bind((BIND_HOST, first))
        occupied.listen(1)
        ctx = _ctx(tmp_path)
        server, port = bind(ctx, first)
        try:
            assert port == first + 1
            # The gate compares against the *bound* port, so the context must follow.
            assert ctx.port == port
        finally:
            server.server_close()


def test_binding_refuses_after_the_walk_limit(tmp_path):
    """Bounded rather than walking forever, so a wedged machine gets a real error."""
    start = _free_port()
    held = []
    try:
        for offset in range(PORT_WALK_LIMIT):
            sock = socket.socket()
            try:
                sock.bind((BIND_HOST, start + offset))
                sock.listen(1)
                held.append(sock)
            except OSError:
                sock.close()
        if len(held) < PORT_WALK_LIMIT:
            pytest.skip("could not occupy a contiguous port range on this machine")
        with pytest.raises(OSError, match="--port"):
            bind(_ctx(tmp_path), start)
    finally:
        for sock in held:
            sock.close()


def test_the_default_port_avoids_the_json_rpc_server():
    """Colliding with `garuda serve` would make a stale-port failure look like an
    authentication failure."""
    assert DEFAULT_PORT == 8787 != 8765


def test_a_token_is_generated_and_carried_in_the_url_fragment(tmp_path, monkeypatch):
    monkeypatch.delenv(TOKEN_ENV_VAR, raising=False)
    ctx = build_context(DashboardConfig(port=9999, sessions_dir=tmp_path))
    assert len(ctx.token) >= 20
    url = launch_url(ctx)
    # A fragment, so the token is never sent to a server, never logged, never in Referer.
    assert url == f"http://127.0.0.1:9999/#t={ctx.token}"
    assert "?" not in url


def test_the_token_env_var_overrides_the_generated_one(tmp_path, monkeypatch):
    monkeypatch.setenv(TOKEN_ENV_VAR, "a-fixed-token")
    ctx = build_context(DashboardConfig(sessions_dir=tmp_path))
    assert ctx.token == "a-fixed-token"


def test_an_explicit_config_token_wins_over_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv(TOKEN_ENV_VAR, "from-env")
    ctx = build_context(DashboardConfig(token="explicit", sessions_dir=tmp_path))
    assert ctx.token == "explicit"


async def test_the_printed_url_reaches_a_redirected_stdout(tmp_path, monkeypatch):
    """`flush=True` is load-bearing: stdout block-buffers when it is not a tty, and
    this process then blocks in serve_forever without ever filling the buffer — so
    the only copy of the token would be stranded in memory."""
    import asyncio

    from garuda.interfaces import web as web_module

    monkeypatch.delenv(TOKEN_ENV_VAR, raising=False)
    port = _free_port()
    config = DashboardConfig(port=port, sessions_dir=tmp_path, open_browser=False)
    captured = io.StringIO()

    async def run_briefly():
        task = asyncio.create_task(web_module.serve_dashboard(config))
        # Yield until the print has happened, then stop.
        for _ in range(50):
            await asyncio.sleep(0.01)
            if captured.getvalue():
                break
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    with contextlib.redirect_stdout(captured):
        await run_briefly()

    printed = captured.getvalue()
    assert f"http://127.0.0.1:{port}/#t=" in printed, printed
    # `read-write` is the default now: talking to an agent is the point of the dashboard, and
    # what bounds the risk is the permission ceiling and the token, not this switch.
    assert "read-write" in printed
    assert str(tmp_path) in printed


async def test_serving_reports_the_bound_port_not_the_requested_one(tmp_path, monkeypatch):
    """Printing the asked-for port after walking hands the user a URL that 403s."""
    import asyncio

    from garuda.interfaces import web as web_module

    monkeypatch.delenv(TOKEN_ENV_VAR, raising=False)
    first = _free_port()
    captured = io.StringIO()
    with socket.socket() as occupied:
        occupied.bind((BIND_HOST, first))
        occupied.listen(1)
        config = DashboardConfig(port=first, sessions_dir=tmp_path, open_browser=False)

        async def run_briefly():
            task = asyncio.create_task(web_module.serve_dashboard(config))
            for _ in range(50):
                await asyncio.sleep(0.01)
                if captured.getvalue():
                    break
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

        with contextlib.redirect_stdout(captured):
            await run_briefly()

    assert f":{first + 1}/" in captured.getvalue(), captured.getvalue()
