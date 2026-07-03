"""D7b: HTTP/SSE MCP transport — config inference + transport selection."""

import json
from contextlib import AsyncExitStack
from pathlib import Path

import pytest

import garuda.mcp.client as client_mod
from garuda.mcp.client import McpClientManager
from garuda.mcp.config import load_mcp_config, normalize_transport


# --- transport normalization ------------------------------------------------

def test_normalize_transport_aliases():
    assert normalize_transport("streamable-http") == "http"
    assert normalize_transport("streamable_http") == "http"
    assert normalize_transport("STREAMABLEHTTP") == "http"
    assert normalize_transport("SSE") == "sse"
    assert normalize_transport("stdio") == "stdio"


def test_normalize_transport_infers_from_url():
    assert normalize_transport(None, has_url=True) == "http"
    assert normalize_transport(None, has_url=False) == "stdio"
    assert normalize_transport("", has_url=True) == "http"


# --- config parsing ---------------------------------------------------------

def _write(tmp_path: Path, name: str, data: dict) -> str:
    p = tmp_path / name
    p.write_text(json.dumps(data), encoding="utf-8")
    return str(p)


def test_url_entry_infers_http_transport(tmp_path: Path):
    path = _write(tmp_path, "mcp.json", {
        "mcpServers": {"api": {"url": "https://example.com/mcp"}}
    })
    (cfg,) = load_mcp_config(path)
    assert cfg.name == "api"
    assert cfg.transport == "http"
    assert cfg.url == "https://example.com/mcp"


def test_explicit_sse_type(tmp_path: Path):
    path = _write(tmp_path, "mcp.json", {
        "mcpServers": {"stream": {"type": "sse", "url": "https://example.com/sse"}}
    })
    (cfg,) = load_mcp_config(path)
    assert cfg.transport == "sse"


def test_headers_and_bearer_shorthand(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("MY_TOKEN", "secret123")
    path = _write(tmp_path, "mcp.json", {
        "mcpServers": {
            "withhdr": {"url": "https://x/mcp", "headers": {"X-Api-Key": "${MY_TOKEN}"}},
            "withauth": {"url": "https://y/mcp", "auth": "${MY_TOKEN}"},
        }
    })
    cfgs = {c.name: c for c in load_mcp_config(path)}
    assert cfgs["withhdr"].headers["X-Api-Key"] == "secret123"
    assert cfgs["withauth"].headers["Authorization"] == "Bearer secret123"


def test_stdio_still_default(tmp_path: Path):
    path = _write(tmp_path, "mcp.json", {
        "mcpServers": {"local": {"command": "echo", "args": ["hi"]}}
    })
    (cfg,) = load_mcp_config(path)
    assert cfg.transport == "stdio"
    assert cfg.command == "echo"


# --- transport selection in the client --------------------------------------

class _FakeCtx:
    def __init__(self, value):
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, *exc):
        return False


async def test_open_transport_http_selects_streamablehttp(monkeypatch):
    calls = {}

    def fake_http(url, headers=None, timeout=None):
        calls["url"] = url
        calls["headers"] = headers
        calls["timeout"] = timeout
        return _FakeCtx(("R", "W", lambda: "sid"))  # streamable-HTTP yields a 3rd element

    monkeypatch.setattr(client_mod, "streamablehttp_client", fake_http)
    from garuda.mcp.config import McpServerConfig

    server = McpServerConfig(
        name="api", transport="http", url="https://x/mcp", headers={"Authorization": "Bearer t"}
    )
    async with AsyncExitStack() as stack:
        read, write = await McpClientManager()._open_transport(server, stack)
    assert (read, write) == ("R", "W")  # the 3rd element is dropped
    assert calls["url"] == "https://x/mcp"
    assert calls["headers"] == {"Authorization": "Bearer t"}
    assert calls["timeout"] == client_mod.MCP_HTTP_CONNECT_TIMEOUT


async def test_open_transport_sse_selects_sse_client(monkeypatch):
    calls = {}

    def fake_sse(url, headers=None, timeout=None):
        calls["url"] = url
        return _FakeCtx(("R2", "W2"))

    monkeypatch.setattr(client_mod, "sse_client", fake_sse)
    from garuda.mcp.config import McpServerConfig

    server = McpServerConfig(name="stream", transport="sse", url="https://x/sse")
    async with AsyncExitStack() as stack:
        read, write = await McpClientManager()._open_transport(server, stack)
    assert (read, write) == ("R2", "W2")
    assert calls["url"] == "https://x/sse"


async def test_open_transport_http_without_url_raises():
    from garuda.mcp.config import McpServerConfig

    server = McpServerConfig(name="bad", transport="http")
    async with AsyncExitStack() as stack:
        with pytest.raises(ValueError, match="no url"):
            await McpClientManager()._open_transport(server, stack)


async def test_open_transport_unknown_raises():
    from garuda.mcp.config import McpServerConfig

    server = McpServerConfig(name="bad", transport="carrier-pigeon")
    async with AsyncExitStack() as stack:
        with pytest.raises(ValueError, match="Unknown MCP transport"):
            await McpClientManager()._open_transport(server, stack)


async def test_dead_http_sse_servers_are_isolated_not_fatal():
    """A failed HTTP/SSE connect (incl. CancelledError from the transport's inner
    anyio scope) must be isolated per-server, never abort start() or the run."""
    from garuda.mcp.config import McpServerConfig

    mgr = McpClientManager()
    # Real streamablehttp_client / sse_client paths against a closed port.
    await mgr.start(
        [
            McpServerConfig(name="dead_http", transport="http", url="http://127.0.0.1:1/mcp"),
            McpServerConfig(name="dead_sse", transport="sse", url="http://127.0.0.1:1/sse"),
        ]
    )
    assert mgr.get_tools() == []
    await mgr.close()
