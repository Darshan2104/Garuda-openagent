"""SDK and JSON-RPC runtime API tests for issue #39 (P1.8).

Old SDK construction still yields native runs; explicit runtimes execute ACP
turns with wrapped results; conversations hold ACP sessions across turns; the
server exposes versioned runtime methods with per-request manifests.
"""

import sys
from pathlib import Path

import pytest

from garuda.interfaces.runtime_cli import RUNTIME_API_VERSION
from garuda.interfaces.server import JsonRpcServer, ServerConfig
from garuda.sdk.conversation import Conversation
from garuda.sdk.software_agent import SoftwareAgent

FAKE_ARGV = [sys.executable, "-m", "garuda.acp.fake_agent", "--profile", "success"]


@pytest.fixture(autouse=True)
def trusted_runtime_settings(monkeypatch):
    """Make fake homes emulate the strict global runtime settings loader."""
    from garuda.acp import catalog
    from garuda.config import agent_home

    monkeypatch.setattr(
        catalog,
        "load_trusted_runtime_settings",
        lambda: dict(getattr(agent_home.resolve_agent_home("."), "global_settings", {})),
    )


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

    async def refuse_native_assembly(*args, **kwargs):
        pytest.fail("External runtimes must not assemble native tools, including editing fan-out")

    monkeypatch.setattr("garuda.sdk.software_agent.prepare_agent_run", refuse_native_assembly)
    monkeypatch.setattr("garuda.core.loop.prepare_run", refuse_native_assembly)
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
    with pytest.raises(ValueError, match="Cannot use runtime"):
        await agent.run("x")


@pytest.mark.parametrize("last_turn", ["completed", "failed", "cancelled"])
async def test_conversation_holds_and_settles_acp_session(tmp_path, monkeypatch, last_turn):
    import os
    import signal

    from garuda.acp.protocol import AcpCancelledError, AcpError
    from garuda.config import agent_home
    from garuda.core.sessions import SessionStore

    runtimes = _fake_extra()
    if last_turn == "cancelled":
        runtimes[0]["command"] = [*FAKE_ARGV[:-1], "cancel-stop"]
    monkeypatch.setattr(
        agent_home, "resolve_agent_home", lambda workspace: _FakeHome(runtimes)
    )
    store = SessionStore()
    conversation = Conversation(workspace=tmp_path, runtime="faketest", store=store)
    final_message = ""
    try:
        if last_turn == "cancelled":
            with pytest.raises(AcpCancelledError):
                await conversation.run("cancel this turn")
            (record,) = store.list_sessions()
            session_id = record["session_id"]
        else:
            first = await conversation.run("turn one")
            session_id = first.metadata["session_id"]
            assert first.turns == 1 and conversation.trail()
            if last_turn == "failed":
                (child,) = store.load_meta(session_id)["runtime_children"]
                os.kill(child["pid"], signal.SIGTERM)
                with pytest.raises(AcpError):
                    await conversation.run("turn two")
            else:
                second = await conversation.run("turn two")
                assert second.turns == 2 and second.metadata["session_id"] == session_id
                final_message = second.final_message
                assert "turn two" in final_message
    finally:
        await conversation.close()

    meta = store.load_meta(session_id)
    assert meta["state"]["outcome"] == last_turn
    assert meta["state"]["work"] == ("stopped" if last_turn == "cancelled" else "done")
    assert meta["state"]["process"] == "exited"
    assert meta["state"]["verification"] == {"status": "unavailable"}
    assert meta["final_message"] == final_message
    assert meta["turns"] == (1 if last_turn == "cancelled" else 2)
    await conversation.close()
    assert store.load_meta(session_id) == meta


async def test_closing_a_pending_acp_turn_records_cancellation(tmp_path, monkeypatch):
    import asyncio

    from garuda.config import agent_home
    from garuda.core.sessions import SessionStore

    runtimes = _fake_extra()
    runtimes[0]["command"] = [*FAKE_ARGV[:-1], "approval"]
    monkeypatch.setattr(agent_home, "resolve_agent_home", lambda workspace: _FakeHome(runtimes))
    approval_seen = asyncio.Event()

    async def await_approval(_action):
        approval_seen.set()
        await asyncio.Event().wait()

    store = SessionStore()
    conversation = Conversation(workspace=tmp_path, runtime="faketest", store=store,
                                approval_handler=await_approval)
    turn = asyncio.create_task(conversation.run("List the README files"))
    try:
        await asyncio.wait_for(approval_seen.wait(), 5)
        (record,) = store.list_sessions()
        await asyncio.wait_for(asyncio.gather(conversation.close(), conversation.close()), 5)
        with pytest.raises(asyncio.CancelledError):
            await turn
        updated = store.load_meta(record["session_id"])
        assert updated["state"]["outcome"] == "cancelled"
        (child,) = updated["runtime_children"]
        assert child["state"] == "exited"
    finally:
        if not turn.done():
            turn.cancel()
        await asyncio.gather(turn, return_exceptions=True)
        await conversation.close()


class _FakeHome:
    def __init__(self, runtimes):
        self.global_settings = {"runtimes": runtimes}
        self.settings = {}
        self.workspace = Path(".")


async def test_server_runtime_methods_are_versioned_and_isolated(monkeypatch):
    from garuda.config import agent_home

    monkeypatch.setattr(
        agent_home, "resolve_agent_home", lambda workspace: _FakeHome(_fake_extra())
    )
    server = JsonRpcServer(ServerConfig(token=None))
    listed = await server.handle(
        {"jsonrpc": "2.0", "id": 1, "method": "runtime_list",
         "params": {}}, {}
    )
    assert listed["result"]["api"] == "runtime/v1"
    ids = {r["runtime_id"] for r in listed["result"]["runtimes"]}
    assert {"native", "faketest"} <= ids

    inspected = await server.handle(
        {"jsonrpc": "2.0", "id": 2, "method": "runtime_inspect",
         "params": {"runtime": "faketest"}}, {}
    )
    assert inspected["result"]["api"] == "runtime/v1"
    assert inspected["result"]["runtime_id"] == "faketest"

    unknown = await server.handle(
        {"jsonrpc": "2.0", "id": 3, "method": "runtime_inspect",
         "params": {"runtime": "nope"}}, {}
    )
    assert "error" in unknown

    injected = await server.handle(
        {"jsonrpc": "2.0", "id": 4, "method": "runtime_list",
         "params": {"runtimes": _fake_extra()}}, {}
    )
    assert "error" in injected


async def test_server_handoff_preview_and_recover(tmp_path, monkeypatch):
    from garuda.config import agent_home
    from garuda.core.sessions import SessionStore

    monkeypatch.setenv("GARUDA_SESSIONS_DIR", str(tmp_path / "sessions"))
    monkeypatch.setattr(
        agent_home, "resolve_agent_home", lambda workspace: _FakeHome(_fake_extra())
    )
    store = SessionStore()
    store.begin("s1", task="t", model="m", agent="a", workspace=str(tmp_path))
    store.checkpoint_messages("s1", [])
    store.ensure_unified("s1")
    from garuda.workspace.diff import capture_baseline

    store.record_baseline("s1", capture_baseline(str(tmp_path)).to_dict())
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
    assert prepared["result"]["acknowledged"] is True
    assert "acknowledged" in prepared["result"]["detail"]
    assert store.load_unified("s1").handoff["state"] == "acknowledged"

    report = await server.handle(
        {"jsonrpc": "2.0", "id": 3, "method": "runtime_recover",
         "params": {"session": "s1"}}, {}
    )
    assert report["result"]["api"] == "runtime/v1"
    assert report["result"]["resume_session_id"] == "s1"


def _fake_approval_extra():
    return [
        {
            "runtime_id": "fakeapproval",
            "kind": "acp",
            "command": [
                sys.executable, "-m", "garuda.acp.fake_agent",
                "--profile", "approval",
            ],
            "version": "1",
            "setup": "fake",
        }
    ]


async def test_sdk_and_server_select_the_same_runtime(monkeypatch):
    """Parity: the SDK selector and the versioned JSON-RPC methods resolve
    the same runtime id for the same input — native default included."""
    from garuda.acp.catalog import shared_registry
    from garuda.config import agent_home

    monkeypatch.setattr(
        agent_home, "resolve_agent_home", lambda workspace: _FakeHome(_fake_extra())
    )
    registry = shared_registry(
        extra_manifests=_fake_extra(), include_builtins=False, disabled=frozenset()
    )
    assert SoftwareAgent(workspace=".", runtime="native").runtime_name == "native"
    assert registry.get("native").runtime_id == "native"
    assert registry.get("faketest").runtime_id == "faketest"

    server = JsonRpcServer(ServerConfig(token=None))
    listed = await server.handle(
        {"jsonrpc": "2.0", "id": 1, "method": "runtime_list",
         "params": {}}, {}
    )
    ids = {r["runtime_id"] for r in listed["result"]["runtimes"]}
    assert {"native", "faketest"} <= ids
    assert listed["result"]["api"] == "runtime/v1"


async def test_sdk_acp_run_attaches_unified_session(tmp_path, monkeypatch):
    from garuda.config import agent_home
    from garuda.core.sessions import SessionStore

    monkeypatch.setattr(
        agent_home, "resolve_agent_home", lambda workspace: _FakeHome(_fake_extra())
    )
    store = SessionStore(tmp_path / "sessions")
    agent = SoftwareAgent(workspace=".", runtime="faketest", store=store)
    result = await agent.run("hello via sdk")
    assert result.success
    session_id = result.metadata["session_id"]
    unified = store.load_unified(session_id)
    assert [s.runtime_id for s in unified.segments] == ["native", "faketest"]
    assert unified.active.kind == "acp"
    assert unified.active.native_session_id


async def test_sdk_disabled_runtime_is_refused_before_launch(tmp_path, monkeypatch):
    from garuda.config import agent_home
    from garuda.core.sessions import SessionStore

    home_settings = {"runtimes": _fake_extra(), "disabled_runtimes": ["faketest"]}
    monkeypatch.setattr(
        agent_home, "resolve_agent_home", lambda workspace: _FakeHomeSettings(home_settings)
    )
    store = SessionStore(tmp_path / "sessions")
    agent = SoftwareAgent(workspace=".", runtime="faketest", store=store)
    from garuda.runtime import RegistryError

    with pytest.raises(RegistryError, match="disabled"):
        await agent.run("x")


async def test_conversation_switch_runs_the_transaction(tmp_path, monkeypatch):
    from garuda.config import agent_home
    from garuda.core.sessions import SessionStore

    extra = _fake_extra() + [
        {
            "runtime_id": "faketest2",
            "kind": "acp",
            "command": FAKE_ARGV,
            "version": "1",
            "setup": "fake",
        }
    ]
    monkeypatch.setattr(
        agent_home, "resolve_agent_home", lambda workspace: _FakeHome(extra)
    )
    store = SessionStore(tmp_path / "sessions")
    conversation = Conversation(workspace=".", runtime="faketest", store=store)
    first = await conversation.run("turn one")
    assert first.success
    assert conversation._runtime_name == "faketest"
    summary = await conversation.switch_runtime("faketest2")
    assert summary["phase"] == "acknowledged"
    assert summary["previous_runtime"] == "faketest"
    assert conversation._runtime_name == "faketest2"
    unified = store.load_unified(summary["session_id"])
    assert [s.runtime_id for s in unified.segments] == ["native", "faketest", "faketest2"]
    assert unified.handoff["state"] == "acknowledged"
    second = await conversation.run("turn two")
    assert second.success
    await conversation.close()
    with pytest.raises(Exception, match="disabled|unknown|Cannot use"):
        await conversation.switch_runtime("nope")


async def test_conversation_relays_approvals_to_handler(tmp_path, monkeypatch):
    from garuda.config import agent_home
    from garuda.core.sessions import SessionStore

    monkeypatch.setattr(
        agent_home, "resolve_agent_home",
        lambda workspace: _FakeHome(_fake_approval_extra()),
    )
    store = SessionStore(tmp_path / "sessions")
    answers: list[str] = []

    async def _allow(action: str) -> bool:
        answers.append(action)
        return True

    conversation = Conversation(
        workspace=".", runtime="fakeapproval", store=store, approval_handler=_allow
    )
    result = await conversation.run("delete it")
    assert result.success
    assert answers, "the agent approval request must reach the human handler"
    await conversation.close()


class _FakeHomeSettings:
    def __init__(self, settings):
        self.global_settings = settings
        self.settings = {}
        self.workspace = Path(".")
