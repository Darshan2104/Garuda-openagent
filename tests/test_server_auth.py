"""Tests for JSON-RPC server bearer-token auth and the sessions method."""


import pytest

from garuda.core.sessions import SessionStore
from garuda.interfaces.server import (
    UNAUTHORIZED_CODE,
    JsonRpcServer,
    ServerConfig,
    ensure_secure_config,
)

HEALTH = {"jsonrpc": "2.0", "method": "health", "id": 1}


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
