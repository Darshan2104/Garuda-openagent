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
    assert meta["status"] == ("success" if last_turn == "completed" else last_turn)
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


async def test_failed_acp_start_closes_the_adapter_and_retries_cleanly(tmp_path, monkeypatch):
    from garuda.acp.protocol import AcpProtocolError
    from garuda.config import agent_home
    from garuda.core.sessions import SessionStore
    from garuda.interfaces import runtime_cli

    monkeypatch.setattr(agent_home, "resolve_agent_home", lambda workspace: _FakeHome(_fake_extra()))
    real_factory = runtime_cli.acp_adapter_for_workspace
    adapters = []

    def failing_once(*args, **kwargs):
        name, adapter = real_factory(*args, **kwargs)
        adapters.append(adapter)
        if len(adapters) == 1:
            closed = []
            real_close = adapter.close

            async def record_close():
                closed.append(True)
                await real_close()

            adapter._argv = [*FAKE_ARGV[:-1], "version-mismatch"]
            adapter.close = record_close
            adapter.closed = closed
        return name, adapter

    monkeypatch.setattr(runtime_cli, "acp_adapter_for_workspace", failing_once)
    store = SessionStore()
    conversation = Conversation(workspace=tmp_path, runtime="faketest", store=store)
    try:
        with pytest.raises(AcpProtocolError, match="version mismatch"):
            await conversation.run("turn one")
        (failed,) = store.list_sessions()
        assert adapters[0].closed == [True]
        failed_meta = store.load_meta(failed["session_id"])
        assert failed_meta["state"]["outcome"] == "failed"
        assert failed_meta["turns"] == 0

        retried = await conversation.run("turn two")
        assert retried.success is True and "turn two" in retried.final_message
        assert retried.metadata["session_id"] != failed["session_id"]
        assert len(adapters) == 2
    finally:
        await conversation.close()
    assert store.load_meta(retried.metadata["session_id"])["state"]["outcome"] == "completed"
    assert store.load_meta(failed["session_id"]) == failed_meta


class _StartFailingAdapter:
    """An adapter whose start fails and whose close fails or is slow (#629)."""

    runtime_id = "faketest"

    def __init__(self, close_error=None):
        import asyncio

        self.close_error = close_error
        self.close_entered = asyncio.Event()
        self.release_close = asyncio.Event()
        self.release_close.set()
        self.reaped = False

    async def start(self, **_kwargs):
        from garuda.acp.protocol import AcpProtocolError

        raise AcpProtocolError("version mismatch")

    async def close(self):
        self.close_entered.set()
        await self.release_close.wait()
        if self.close_error is not None:
            raise self.close_error
        self.reaped = True


def _serve_adapter(monkeypatch, adapter):
    from garuda.config import agent_home
    from garuda.interfaces import runtime_cli

    monkeypatch.setattr(agent_home, "resolve_agent_home", lambda workspace: _FakeHome(_fake_extra()))
    monkeypatch.setattr(runtime_cli, "acp_adapter_for_workspace", lambda *a, **k: ("faketest", adapter))


async def test_failed_acp_start_keeps_the_start_error_when_close_fails(tmp_path, monkeypatch, caplog):
    from garuda.acp.protocol import AcpProtocolError
    from garuda.core.sessions import SessionStore

    _serve_adapter(monkeypatch, _StartFailingAdapter(close_error=BrokenPipeError("harness gone")))
    store = SessionStore()
    conversation = Conversation(workspace=tmp_path, runtime="faketest", store=store)
    with caplog.at_level("WARNING", logger="garuda.sdk.conversation"):
        with pytest.raises(AcpProtocolError, match="version mismatch"):
            await conversation.run("turn one")
    assert "harness gone" in caplog.text
    (failed,) = store.list_sessions()
    assert store.load_meta(failed["session_id"])["state"]["outcome"] == "failed"
    await conversation.close()


async def test_close_during_failed_acp_start_still_reaps_the_harness(tmp_path, monkeypatch):
    import asyncio

    from garuda.acp.protocol import AcpProtocolError
    from garuda.core.sessions import SessionStore

    adapter = _StartFailingAdapter()
    adapter.release_close.clear()
    _serve_adapter(monkeypatch, adapter)
    store = SessionStore()
    conversation = Conversation(workspace=tmp_path, runtime="faketest", store=store)
    turn = asyncio.create_task(conversation.run("turn one"))
    try:
        await asyncio.wait_for(adapter.close_entered.wait(), 5)
        closing = asyncio.create_task(conversation.close())
        await asyncio.sleep(0.05)
        adapter.release_close.set()
        await asyncio.wait_for(closing, 5)
        assert adapter.reaped is True
        with pytest.raises(AcpProtocolError, match="version mismatch"):
            await turn
        (record,) = store.list_sessions()
        assert store.load_meta(record["session_id"])["state"]["outcome"] == "cancelled"
    finally:
        if not turn.done():
            turn.cancel()
        await asyncio.gather(turn, return_exceptions=True)
        await conversation.close()


def _mode_runtime(monkeypatch, *, proven: bool) -> None:
    """A fake harness that starts in a mode broader than an SDK run allows (#700)."""
    from garuda.config import agent_home
    from garuda.runtime import roles

    runtimes = [{"runtime_id": "fakemode", "kind": "acp", "version": "1", "setup": "fake",
                 "command": [*FAKE_ARGV[:-1], "config-options", "--initial-mode", "judge",
                             "--agent-info", '{"version": "1"}']}]
    monkeypatch.setattr(agent_home, "resolve_agent_home", lambda workspace: _FakeHome(runtimes))
    monkeypatch.setitem(roles.MODE_AUTHORITY, "fakemode",
                        {"ask": "readonly", "edit": "smart", "judge": "auto", "everything": "yolo"})
    monkeypatch.setitem(roles.DEFAULT_MODES, "fakemode", {"readonly": "ask", "smart": "edit"})
    if proven:
        monkeypatch.setitem(roles.PROVEN_OPTIONS, ("fakemode", "1"), {"harness_mode": "mode"})


@pytest.mark.parametrize("surface", ["conversation", "software_agent"])
async def test_sdk_acp_refuses_an_unenforceable_broader_mode_before_the_prompt(
        tmp_path, monkeypatch, surface):
    from garuda.core.sessions import SessionStore
    from garuda.runtime.roles import RoleRefused

    _mode_runtime(monkeypatch, proven=False)
    store = SessionStore(tmp_path / "sessions")
    asked: list[str] = []

    async def deny(action: str) -> bool:
        asked.append(action)
        return False

    if surface == "conversation":
        runner = Conversation(workspace=tmp_path, runtime="fakemode", store=store,
                              approval_handler=deny)
    else:
        runner = SoftwareAgent(workspace=str(tmp_path), runtime="fakemode", store=store)
    try:
        with pytest.raises(RoleRefused) as caught:
            await runner.run("write outside the workspace")
    finally:
        if surface == "conversation":
            await runner.close()
    assert caught.value.code == "role.mode_unenforced"
    assert "runs in mode 'judge', which needs permissions: auto" in str(caught.value)
    (meta,) = store.list_sessions()
    assert not meta.get("final_message") and "acp_mode" not in meta


async def test_sdk_conversation_sets_and_records_the_mode_on_a_proven_adapter(tmp_path, monkeypatch):
    from garuda.core.sessions import SessionStore

    _mode_runtime(monkeypatch, proven=True)
    store = SessionStore(tmp_path / "sessions")
    conversation = Conversation(workspace=tmp_path, runtime="fakemode", store=store)
    try:
        result = await conversation.run("hello")
    finally:
        await conversation.close()
    assert "done: hello" in result.final_message
    meta = store.load_meta(result.metadata["session_id"])
    assert meta["acp_mode"] == {"mode": "edit", "authority": "smart", "required": "edit",
                                "source": "garuda"}

class _SecretTool:
    name = "secret_word"
    description = "Returns the secret word"
    parameters = {"type": "object", "properties": {}}

    async def execute(self, arguments, env, ctx):
        from garuda.types import ToolResult

        return ToolResult(tool_call_id="", content="MANGO-42")


@pytest.mark.parametrize("via", ["run", "conversation", "project_tools"])
async def test_sdk_refuses_tools_an_acp_runtime_cannot_receive(tmp_path, monkeypatch, via):
    """#702: a registered tool is refused before launch, never silently dropped."""
    from garuda.config import agent_home
    from garuda.core.sessions import SessionStore
    from garuda.interfaces import runtime_cli
    from garuda.sdk import ToolsUnsupportedOnRuntime

    monkeypatch.setattr(agent_home, "resolve_agent_home", lambda workspace: _FakeHome(_fake_extra()))

    def no_launch(*args, **kwargs):
        pytest.fail("a refused run must not construct the harness")

    monkeypatch.setattr(runtime_cli, "acp_adapter_for_workspace", no_launch)
    store = SessionStore(tmp_path / "sessions")
    agent = SoftwareAgent(workspace=tmp_path, runtime="faketest", store=store,
                          load_project_tools=True if via == "project_tools" else None)
    if via != "project_tools":
        agent.register_tool(_SecretTool())
    with pytest.raises(ToolsUnsupportedOnRuntime) as refused:
        if via == "conversation":
            conversation = agent.conversation()
            try:
                await conversation.run("call secret_word")
            finally:
                await conversation.close()
        else:
            await agent.run("call secret_word")
    assert refused.value.code == "sdk.tools_unsupported"
    assert isinstance(refused.value, ValueError)
    assert ("secret_word" if via != "project_tools" else "load_project_tools") in str(refused.value)
    assert store.list_sessions() == []


@pytest.mark.parametrize("via", ["run", "conversation"])
async def test_sdk_acp_session_records_the_harness_and_its_usage(tmp_path, monkeypatch, via):
    """#702: meta names the harness that ran (as the CLI does) and the turn's
    usage report reaches the usage ledger."""
    from garuda.config import agent_home
    from garuda.core.sessions import SessionStore
    from garuda.observability.ledger import Ledger

    runtimes = _fake_extra()
    runtimes[0]["command"] = [*FAKE_ARGV, "--report-usage"]
    monkeypatch.setattr(agent_home, "resolve_agent_home", lambda workspace: _FakeHome(runtimes))
    store = SessionStore(tmp_path / "sessions")
    agent = SoftwareAgent(workspace=tmp_path, runtime="faketest", store=store)
    if via == "run":
        result = await agent.run("hello via sdk")
        session_id = result.metadata["session_id"]
    else:
        conversation = agent.conversation()
        try:
            result = await conversation.run("turn one")
            await conversation.run("turn two")
            session_id = result.metadata["session_id"]
        finally:
            await conversation.close()
    meta = store.load_meta(session_id)
    assert (meta["model"], meta["agent"]) == ("acp:faketest", "faketest")
    assert meta["status"] == "success"
    rows = [r for r in Ledger().records() if r.get("session_id") == session_id]
    assert rows, "an SDK ACP turn must write a usage-ledger row like a CLI ACP run"
    assert {r["kind"] for r in rows} == {"acp_usage_snapshot"}
    assert {(r["model"], r["cost_usd"], r["harness"]) for r in rows} == {
        ("fake-model-1", 0.0025, "faketest")}


async def test_failed_sdk_acp_run_settles_its_session(tmp_path, monkeypatch):
    from garuda.acp.protocol import AcpError
    from garuda.config import agent_home
    from garuda.core.sessions import SessionStore

    runtimes = _fake_extra()
    runtimes[0]["command"] = [*FAKE_ARGV[:-1], "remote-error"]
    monkeypatch.setattr(agent_home, "resolve_agent_home", lambda workspace: _FakeHome(runtimes))
    store = SessionStore(tmp_path / "sessions")
    with pytest.raises(AcpError):
        await SoftwareAgent(workspace=tmp_path, runtime="faketest", store=store).run("x")
    (record,) = store.list_sessions()
    meta = store.load_meta(record["session_id"])
    assert (meta["status"], meta["state"]["outcome"]) == ("failed", "failed")


@pytest.mark.parametrize("option", [{"model": "openai/gpt-x"}, {"reasoning_model": "openai/gpt-x"},
                                    {"collection_model": "openai/gpt-x"}, {"model_binding": "fast"},
                                    {"agent": "plan"},
                                    {"agent": {"extends": "garuda/build"}}],
                         ids=["model", "reasoning_model", "collection_model", "model_binding",
                              "agent_name", "agent_spec"])
@pytest.mark.parametrize("via", ["run", "conversation"])
async def test_sdk_refuses_model_or_agent_an_acp_runtime_would_ignore(tmp_path, monkeypatch, via,
                                                                      option):
    """#712: a model or agent the harness cannot apply is refused before launch."""
    from garuda.config import agent_home
    from garuda.core.sessions import SessionStore
    from garuda.interfaces import runtime_cli
    from garuda.sdk import OptionsUnsupportedOnRuntime

    store = SessionStore(tmp_path / "sessions")
    # A mapping is resolved into a frozen AgentSpec at construction.
    agent = SoftwareAgent(workspace=tmp_path, runtime="faketest", store=store, **option)
    monkeypatch.setattr(agent_home, "resolve_agent_home", lambda workspace: _FakeHome(_fake_extra()))

    def no_launch(*args, **kwargs):
        pytest.fail("a refused run must not construct the harness")

    monkeypatch.setattr(runtime_cli, "acp_adapter_for_workspace", no_launch)
    with pytest.raises(OptionsUnsupportedOnRuntime) as refused:
        if via == "conversation":
            conversation = agent.conversation()
            try:
                await conversation.run("hello")
            finally:
                await conversation.close()
        else:
            await agent.run("hello")
    assert refused.value.code == "sdk.options_unsupported"
    assert isinstance(refused.value, ValueError)
    assert next(iter(option)) in str(refused.value)
    assert store.list_sessions() == []


@pytest.mark.parametrize("option", [{"mode": "readonly"}, {"mode": "eval"}, {"mode": "rigorous"},
                                    {"mcp_config": "mcp.json"}],
                         ids=["mode_readonly", "mode_eval", "mode_rigorous", "mcp_config"])
@pytest.mark.parametrize("via", ["run", "conversation"])
async def test_sdk_refuses_mode_or_mcp_config_an_acp_runtime_would_ignore(tmp_path, monkeypatch,
                                                                         via, option):
    """#724: a run mode or MCP config the harness path cannot apply is refused
    before launch; a readonly request is never silently widened to the
    harness's default permission mode."""
    from garuda.config import agent_home
    from garuda.core.sessions import SessionStore
    from garuda.interfaces import runtime_cli
    from garuda.sdk import OptionsUnsupportedOnRuntime

    store = SessionStore(tmp_path / "sessions")
    agent = SoftwareAgent(workspace=tmp_path, runtime="faketest", store=store, **option)
    monkeypatch.setattr(agent_home, "resolve_agent_home", lambda workspace: _FakeHome(_fake_extra()))

    def no_launch(*args, **kwargs):
        pytest.fail("a refused run must not construct the harness")

    monkeypatch.setattr(runtime_cli, "acp_adapter_for_workspace", no_launch)
    with pytest.raises(OptionsUnsupportedOnRuntime) as refused:
        if via == "conversation":
            conversation = agent.conversation()
            try:
                await conversation.run("hello")
            finally:
                await conversation.close()
        else:
            await agent.run("hello")
    assert refused.value.code == "sdk.options_unsupported"
    assert next(iter(option)) in str(refused.value)
    assert store.list_sessions() == []


@pytest.mark.parametrize("mode", [None, "standard", "interactive"])
async def test_sdk_default_modes_still_run_on_an_acp_runtime(tmp_path, monkeypatch, mode):
    """#724: the default posture asks for nothing the harness path ignores."""
    from garuda.config import agent_home
    from garuda.core.sessions import SessionStore

    monkeypatch.setattr(agent_home, "resolve_agent_home", lambda workspace: _FakeHome(_fake_extra()))
    store = SessionStore(tmp_path / "sessions")
    result = await SoftwareAgent(workspace=tmp_path, runtime="faketest", store=store,
                                 mode=mode).run("hello")
    assert result.success
