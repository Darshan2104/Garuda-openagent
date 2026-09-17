"""SDK and JSON-RPC runtime API tests for issue #39 (P1.8).

Old SDK construction still yields native runs; explicit runtimes execute ACP
turns with wrapped results; conversations hold ACP sessions across turns; the
server exposes versioned runtime methods with per-request manifests.
"""

import sys

import pytest

from garuda.interfaces.runtime_cli import RUNTIME_API_VERSION
from garuda.interfaces.server import JsonRpcServer, ServerConfig
from garuda.sdk.conversation import Conversation
from garuda.sdk.software_agent import SoftwareAgent

FAKE_ARGV = [sys.executable, "-m", "garuda.acp.fake_agent", "--profile", "success"]


def _fake_extra() -> list[dict]:
    return [
        {
            "runtime_id": "faketest",
            "kind": "acp",
            "command": FAKE_ARGV,
            "version": "1",
            "setup": "fake",
        }
    ]


def test_old_sdk_construction_stays_native():
    agent = SoftwareAgent(workspace=".")
    assert agent.runtime_name == "native"
    assert agent.conversation()._runtime_name == "native"


def test_runtime_api_is_versioned():
    assert RUNTIME_API_VERSION == "1"


async def test_sdk_acp_run_wraps_result(monkeypatch):
    from garuda.config import agent_home

    monkeypatch.setattr(
        agent_home, "resolve_agent_home", lambda workspace: _FakeHome(_fake_extra())
    )
    agent = SoftwareAgent(workspace=".", runtime="faketest")
    result = await agent.run("hello via sdk")
    assert result.success is True
    assert "done: hello via sdk" in result.final_message
    assert result.metadata["runtime"] == "faketest"
    assert result.metadata["api"] == RUNTIME_API_VERSION


async def test_sdk_unknown_runtime_fails_actionably(monkeypatch):
    from garuda.config import agent_home

    monkeypatch.setattr(
        agent_home, "resolve_agent_home", lambda workspace: _FakeHome([])
    )
    agent = SoftwareAgent(workspace=".", runtime="nope")
    with pytest.raises(ValueError, match="Unknown runtime"):
        await agent.run("x")


async def test_conversation_holds_acp_session(monkeypatch):
    from garuda.config import agent_home

    monkeypatch.setattr(
        agent_home, "resolve_agent_home", lambda workspace: _FakeHome(_fake_extra())
    )
    conversation = Conversation(workspace=".", runtime="faketest")
    first = await conversation.run("turn one")
    second = await conversation.run("turn two")
    assert first.turns == 1
    assert second.turns == 2
    assert len(conversation.trail()) > 0
    assert "turn two" in second.final_message or second.final_message
    assert conversation._acp.native_session_id
    await conversation.close()
    assert conversation._acp is None


class _FakeHome:
    def __init__(self, runtimes):
        self.global_settings = {"runtimes": runtimes}


async def test_server_runtime_methods_are_versioned_and_isolated():
    server = JsonRpcServer(ServerConfig(token=None))
    listed = await server.handle(
        {"jsonrpc": "2.0", "id": 1, "method": "runtime_list",
         "params": {"runtimes": _fake_extra()}}, {}
    )
    assert listed["result"]["api"] == "runtime/v1"
    ids = {r["runtime_id"] for r in listed["result"]["runtimes"]}
    assert {"native", "faketest"} <= ids

    inspected = await server.handle(
        {"jsonrpc": "2.0", "id": 2, "method": "runtime_inspect",
         "params": {"runtime": "faketest", "runtimes": _fake_extra()}}, {}
    )
    assert inspected["result"]["api"] == "runtime/v1"
    assert inspected["result"]["runtime_id"] == "faketest"

    unknown = await server.handle(
        {"jsonrpc": "2.0", "id": 3, "method": "runtime_inspect",
         "params": {"runtime": "nope", "runtimes": _fake_extra()}}, {}
    )
    assert "error" in unknown

    second = await server.handle(
        {"jsonrpc": "2.0", "id": 4, "method": "runtime_list", "params": {}}, {}
    )
    assert "faketest" not in {r["runtime_id"] for r in second["result"]["runtimes"]}


async def test_server_handoff_preview_and_recover(tmp_path, monkeypatch):
    from garuda.core.sessions import SessionStore

    monkeypatch.setenv("GARUDA_SESSIONS_DIR", str(tmp_path / "sessions"))
    store = SessionStore()
    store.begin("s1", task="t", model="m", agent="a", workspace="w")
    store.ensure_unified("s1")
    server = JsonRpcServer(ServerConfig(token=None))

    preview = await server.handle(
        {"jsonrpc": "2.0", "id": 1, "method": "runtime_handoff",
         "params": {"session": "s1", "target": "faketest"}}, {}
    )
    assert preview["result"]["prepared"] is False
    assert store.load_unified("s1").handoff["state"] == "none"

    prepared = await server.handle(
        {"jsonrpc": "2.0", "id": 2, "method": "runtime_handoff",
         "params": {"session": "s1", "target": "faketest", "confirm": True}}, {}
    )
    assert prepared["result"]["prepared"] is True

    report = await server.handle(
        {"jsonrpc": "2.0", "id": 3, "method": "runtime_recover",
         "params": {"session": "s1"}}, {}
    )
    assert report["result"]["api"] == "runtime/v1"
    assert report["result"]["resume_session_id"] == "s1"
