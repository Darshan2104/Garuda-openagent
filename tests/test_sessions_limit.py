"""#792: session list limits are typed and >= 1 in the store, CLI and JSON-RPC."""

import pytest

from garuda.core.sessions import SessionLimitError, SessionStore, validate_session_limit
from garuda.interfaces import main as cli
from garuda.interfaces.server import JsonRpcServer, ServerConfig


def _store(tmp_path, count=3):
    store = SessionStore(root=tmp_path / "sessions")
    for i in range(count):
        store.begin(session_id=f"s{i}", task=f"task {i}", model="script/test", agent="build",
                    workspace=str(tmp_path))
    return store


@pytest.mark.parametrize("value", [0, -1, -5, "3", "abc", 2.0, True, False, None])
def test_validate_rejects_non_positive_and_non_integers(value):
    with pytest.raises(SessionLimitError, match="integer >= 1"):
        validate_session_limit(value)


def test_list_sessions_limits(tmp_path):
    store = _store(tmp_path)
    assert len(store.list_sessions(limit=None)) == 3
    assert len(store.list_sessions(limit=1)) == 1
    assert len(store.list_sessions(limit=100)) == 3
    for bad in (0, -1):
        with pytest.raises(SessionLimitError):
            store.list_sessions(limit=bad)


@pytest.mark.parametrize("value", ["-1", "0", "abc", "1.5"])
def test_cli_limit_rejected_with_exit_2(value, capsys):
    with pytest.raises(SystemExit) as exc:
        cli.build_parser().parse_args(["sessions", f"--limit={value}"])
    assert exc.value.code == 2
    assert "--limit" in capsys.readouterr().err


def test_cli_limit_accepts_positive():
    assert cli.build_parser().parse_args(["sessions", "--limit", "7"]).limit == 7
    assert cli.build_parser().parse_args(["sessions"]).limit == 20


@pytest.mark.parametrize("value", [-1, 0, "abc", "5", 1.5, True])
async def test_rpc_sessions_invalid_limit_is_invalid_params(value):
    server = JsonRpcServer(ServerConfig(token=None))
    resp = await server.handle(
        {"jsonrpc": "2.0", "id": 1, "method": "sessions", "params": {"limit": value}})
    assert resp["error"]["code"] == -32602
    assert "limit must be an integer >= 1" in resp["error"]["message"]
    assert "int()" not in resp["error"]["message"]


async def test_rpc_sessions_valid_limit(tmp_path, monkeypatch):
    store = _store(tmp_path)
    monkeypatch.setattr("garuda.interfaces.server.SessionStore", lambda: store)
    server = JsonRpcServer(ServerConfig(token=None))
    for params, expected in (({"limit": 2}, 2), ({}, 3)):
        resp = await server.handle(
            {"jsonrpc": "2.0", "id": 1, "method": "sessions", "params": params})
        assert len(resp["result"]["sessions"]) == expected
