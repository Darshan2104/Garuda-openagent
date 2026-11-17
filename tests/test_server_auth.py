"""Tests for JSON-RPC server bearer-token auth and the sessions method."""


import asyncio
import contextlib
import json

import pytest

from garuda.core.sessions import SessionStore
from garuda.interfaces.server import (
    MAX_CONNECTIONS,
    MAX_HEADER_CONNECTIONS,
    UNAUTHORIZED_CODE,
    JsonRpcServer,
    ServerConfig,
    ensure_secure_config,
)

HEALTH = {"jsonrpc": "2.0", "method": "health", "id": 1}


class ResponseWriter:
    def __init__(self):
        self.data = bytearray()
        self.closed = False

    def write(self, data):
        self.data.extend(data)

    async def drain(self):
        pass

    def close(self):
        self.closed = True

    async def wait_closed(self):
        pass


@pytest.mark.parametrize("headers", [
    b"", b"Authorization: Bearer wrong\r\n",
    b"Authorization: Bearer sekrit\r\nOrigin: http://evil.test\r\n",
])
async def test_http_auth_refusal_does_not_wait_for_declared_body(headers):
    server = JsonRpcServer(ServerConfig(token="sekrit"))
    reader, writer = asyncio.StreamReader(), ResponseWriter()
    # No body and no EOF: the original attack holds the body read open for 30s.
    reader.feed_data(b"POST / HTTP/1.1\r\n" + headers + b"Content-Length: 33554432\r\n\r\n")
    await asyncio.wait_for(server._connection_handler(reader, writer), 1)
    assert writer.data.startswith(b"HTTP/1.1 401 ")
    response = json.loads(writer.data.split(b"\r\n\r\n", 1)[1])
    assert response["error"]["code"] == UNAUTHORIZED_CODE
    assert response["id"] is None
    assert writer.closed


async def test_http_connection_cap_rejects_excess_and_releases_cancelled_slot():
    server = JsonRpcServer(ServerConfig(token="sekrit"))
    held = []
    try:
        # Authenticated clients hold body admission slots.
        for _ in range(MAX_CONNECTIONS):
            reader, writer = asyncio.StreamReader(), ResponseWriter()
            reader.feed_data(b"POST / HTTP/1.1\r\nAuthorization: Bearer sekrit\r\n"
                             b"Content-Length: 100\r\n\r\n")
            held.append(asyncio.create_task(server._connection_handler(reader, writer)))
        await asyncio.sleep(0)
        reader, writer = asyncio.StreamReader(), ResponseWriter()
        body = json.dumps(HEALTH).encode()
        request = (b"POST / HTTP/1.1\r\nAuthorization: Bearer sekrit\r\n"
                   + f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
        reader.feed_data(request)
        reader.feed_eof()
        await asyncio.wait_for(server._connection_handler(reader, writer), 1)
        assert writer.data.startswith(b"HTTP/1.1 503 ")
        assert writer.closed

        # Refusing excess work must not release a slot owned by another client.
        reader, writer = asyncio.StreamReader(), ResponseWriter()
        reader.feed_data(request)
        reader.feed_eof()
        await asyncio.wait_for(server._connection_handler(reader, writer), 1)
        assert writer.data.startswith(b"HTTP/1.1 503 ")

        held[0].cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await held[0]
        reader, writer = asyncio.StreamReader(), ResponseWriter()
        reader.feed_data(request)
        reader.feed_eof()
        await asyncio.wait_for(server._connection_handler(reader, writer), 1)
        assert writer.data.startswith(b"HTTP/1.1 200 ")
        assert json.loads(writer.data.split(b"\r\n\r\n", 1)[1])["result"]["status"] == "ok"

        # The completed request releases its slot too, with the other authenticated clients still held.
        reader, writer = asyncio.StreamReader(), ResponseWriter()
        reader.feed_data(request)
        reader.feed_eof()
        await asyncio.wait_for(server._connection_handler(reader, writer), 1)
        assert writer.data.startswith(b"HTTP/1.1 200 ")
    finally:
        for task in held:
            task.cancel()
        await asyncio.gather(*held, return_exceptions=True)


@pytest.mark.asyncio
async def test_rejects_missing_bearer_when_token_set():
    server = JsonRpcServer(ServerConfig(token="sekrit"))
    response = await server.handle(dict(HEALTH))
    assert response["error"]["code"] == UNAUTHORIZED_CODE
    assert "result" not in response


@pytest.mark.asyncio
async def test_rejects_wrong_bearer_when_token_set():
    server = JsonRpcServer(ServerConfig(token="sekrit"))
    response = await server.handle(
        dict(HEALTH), headers={"authorization": "Bearer wrong"}
    )
    assert response["error"]["code"] == UNAUTHORIZED_CODE


@pytest.mark.asyncio
async def test_accepts_correct_bearer():
    server = JsonRpcServer(ServerConfig(token="sekrit"))
    response = await server.handle(
        dict(HEALTH), headers={"Authorization": "Bearer sekrit"}
    )
    assert response["result"]["status"] == "ok"


@pytest.mark.asyncio
async def test_no_token_configured_allows_unauthenticated():
    server = JsonRpcServer(ServerConfig())
    response = await server.handle(dict(HEALTH))
    assert response["result"]["status"] == "ok"


def test_refuses_public_host_without_token():
    with pytest.raises(ValueError, match="non-loopback"):
        ensure_secure_config(ServerConfig(host="0.0.0.0", token=None))


def test_public_host_with_explicit_token_ok():
    cfg = ServerConfig(host="0.0.0.0", token="sekrit")
    ensure_secure_config(cfg)
    assert cfg.token == "sekrit"


def test_loopback_auto_generates_token():
    # Loopback with no token must NOT be left unauthenticated (local-RCE surface):
    # a strong token is generated for the session.
    for host in ("127.0.0.1", "localhost", "::1"):
        cfg = ServerConfig(host=host, token=None)
        ensure_secure_config(cfg)
        assert cfg.token and len(cfg.token) >= 20


@pytest.mark.asyncio
async def test_rejects_browser_origin():
    # A request carrying an Origin header (i.e. from a browser) is refused even
    # with a valid token — CSRF / DNS-rebinding defense-in-depth.
    server = JsonRpcServer(ServerConfig(token="sekrit"))
    response = await server.handle(
        dict(HEALTH),
        headers={"authorization": "Bearer sekrit", "origin": "http://evil.example"},
    )
    assert response["error"]["code"] == UNAUTHORIZED_CODE
    assert "cross-origin" in response["error"]["message"]


@pytest.mark.asyncio
async def test_sessions_method_lists_sessions(tmp_path, monkeypatch):
    monkeypatch.setenv("GARUDA_SESSIONS_DIR", str(tmp_path / "sessions"))
    store = SessionStore()
    store.begin(
        session_id="abc12345-0000",
        task="do something",
        model="script/test",
        agent="build",
        workspace=str(tmp_path),
    )

    server = JsonRpcServer(ServerConfig(token="sekrit"))
    response = await server.handle(
        {"jsonrpc": "2.0", "method": "sessions", "id": 2},
        headers={"authorization": "Bearer sekrit"},
    )
    sessions = response["result"]["sessions"]
    assert len(sessions) == 1
    assert sessions[0]["session_id"] == "abc12345-0000"
    assert sessions[0]["status"] == "running"


@pytest.mark.parametrize("value", ["Bearer café", "Bearer \u2603"])
async def test_non_ascii_bearer_is_unauthorized(value):
    server = JsonRpcServer(ServerConfig(token="sekrit"))
    response = await server.handle(dict(HEALTH), headers={"authorization": value})
    assert response["error"]["code"] == UNAUTHORIZED_CODE


@pytest.mark.parametrize("field,value", [
    ("mcp_config", "/tmp/untrusted-mcp.yaml"), ("workspace", "/tmp/other"),
    ("workspace_kind", "local"), ("docker_host", "tcp://attacker:2375"),
    ("docker_image", "attacker/image"), ("agents_dir", "/tmp/agents"),
])
@pytest.mark.parametrize("limits", [{"allowed_agents": ["reviewer"]}, {"permission_ceiling": "readonly"}])
async def test_restricted_server_refuses_request_launch_settings(tmp_path, monkeypatch, field, value, limits):
    import garuda.interfaces.server as server_module

    activated = []

    async def prepare(*args, **kwargs):
        activated.append(kwargs)
        raise RuntimeError("activation reached")

    monkeypatch.setattr(server_module, "prepare_agent_run", prepare)
    server = JsonRpcServer(ServerConfig(token="sekrit", agent="reviewer", workspace=str(tmp_path),
                                        workspace_kind="docker", **limits))
    response = await server.handle({"id": 2, "method": "run", "params": {"task": "t", field: value}},
                                   headers={"authorization": "Bearer sekrit"})
    assert "error" in response
    assert f"params.{field}" in response["error"]["message"]
    assert not activated


@pytest.mark.parametrize("count", [MAX_CONNECTIONS + 1, MAX_HEADER_CONNECTIONS + 1])
async def test_idle_partial_headers_do_not_block_authenticated_health(count):
    server = JsonRpcServer(ServerConfig(token="sekrit"))
    held = []
    try:
        for _ in range(count):
            reader = asyncio.StreamReader()
            reader.feed_data(b"POST / HTTP/1.1\r\n")
            held.append(asyncio.create_task(server._connection_handler(reader, ResponseWriter())))
        await asyncio.sleep(0)
        reader, writer = asyncio.StreamReader(), ResponseWriter()
        body = json.dumps(HEALTH).encode()
        reader.feed_data(b"POST / HTTP/1.1\r\nAuthorization: Bearer sekrit\r\n"
                         + f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
        await asyncio.wait_for(server._connection_handler(reader, writer), 1)
        assert writer.data.startswith(b"HTTP/1.1 200 ")
        assert json.loads(writer.data.split(b"\r\n\r\n", 1)[1])["result"]["status"] == "ok"
    finally:
        for task in held:
            task.cancel()
        await asyncio.gather(*held, return_exceptions=True)


async def test_long_runs_do_not_starve_health_status_or_cancel(monkeypatch):
    server = JsonRpcServer(ServerConfig(token="sekrit"))
    started = asyncio.Queue()
    release = asyncio.Event()

    async def run(params):
        started.put_nowait(True)
        await release.wait()
        return {}

    monkeypatch.setattr(server, "_run", run)

    async def request(method):
        reader, writer = asyncio.StreamReader(), ResponseWriter()
        body = json.dumps({"id": 1, "method": method}).encode()
        reader.feed_data(b"POST / HTTP/1.1\r\nAuthorization: Bearer sekrit\r\n"
                         + f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
        await server._connection_handler(reader, writer)
        return writer

    held = []
    try:
        for _ in range(MAX_CONNECTIONS):
            held.append(asyncio.create_task(request("run")))
            await asyncio.wait_for(started.get(), 1)
        excess = await asyncio.wait_for(request("run"), 1)
        assert excess.data.startswith(b"HTTP/1.1 503 ")
        held[0].cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await held[0]
        held.append(asyncio.create_task(request("run")))
        await asyncio.wait_for(started.get(), 1)
        for method in ("health", "status", "cancel"):
            writer = await asyncio.wait_for(request(method), 1)
            assert writer.data.startswith(b"HTTP/1.1 200 "), method
            response = json.loads(writer.data.split(b"\r\n\r\n", 1)[1])
            if method == "health":
                assert response["result"]["status"] == "ok"
            else:
                assert "job_id" in response["error"]["message"]
    finally:
        release.set()
        await asyncio.gather(*held, return_exceptions=True)


async def test_idle_header_deadline_closes_and_releases_header_capacity(monkeypatch):
    monkeypatch.setattr("garuda.interfaces.server.HEADER_READ_TIMEOUT", 0.01)
    server = JsonRpcServer(ServerConfig(token="sekrit"))
    reader, writer = asyncio.StreamReader(), ResponseWriter()
    reader.feed_data(b"POST / HTTP/1.1\r\n")
    await asyncio.wait_for(server._connection_handler(reader, writer), 1)
    assert writer.closed
    assert not writer.data


async def test_idle_header_sockets_leave_authenticated_health_available():
    server = JsonRpcServer(ServerConfig(token="sekrit"))
    accepted = asyncio.Queue()
    handlers = set()

    async def connection(reader, writer):
        task = asyncio.current_task()
        handlers.add(task)
        accepted.put_nowait(True)
        try:
            await server._connection_handler(reader, writer)
        finally:
            handlers.discard(task)

    try:
        listener = await asyncio.start_server(connection, "127.0.0.1", 0)
    except PermissionError as exc:
        pytest.skip(f"sandbox blocks loopback sockets: {exc}")
    clients = []
    port = listener.sockets[0].getsockname()[1]
    try:
        for _ in range(MAX_CONNECTIONS + 1):
            _, writer = await asyncio.open_connection("127.0.0.1", port)
            clients.append(writer)
            writer.write(b"POST / HTTP/1.1\r\n")
            await writer.drain()
            await asyncio.wait_for(accepted.get(), 1)
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        clients.append(writer)
        body = json.dumps(HEALTH).encode()
        writer.write(b"POST / HTTP/1.1\r\nAuthorization: Bearer sekrit\r\n"
                     + f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
        await writer.drain()
        response = await asyncio.wait_for(reader.read(), 2)
        assert response.startswith(b"HTTP/1.1 200 ")
        assert json.loads(response.split(b"\r\n\r\n", 1)[1])["result"]["status"] == "ok"
    finally:
        listener.close()
        await listener.wait_closed()
        for writer in clients:
            writer.close()
        await asyncio.gather(*(writer.wait_closed() for writer in clients), return_exceptions=True)
        pending = list(handlers)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
