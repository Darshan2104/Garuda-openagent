"""Broker tests for issue #27 (P0.17).

Allow, deny, timeout, and disconnect are all persisted; strict ceilings never
downgrade; unsupported policies are reported, not silently skipped.
"""

import asyncio

import pytest

from garuda.acp.authority import AgentCapabilities, AuthorityPolicy
from garuda.acp.broker import ApprovalBroker, ApprovalOutcome, describe_gaps
from garuda.core.permissions import PermissionEngine
from garuda.core.sessions import SessionStore


def _broker(session_id="s1", store=None, **kwargs) -> ApprovalBroker:
    kwargs.setdefault("timeout_sec", 5.0)
    engine = PermissionEngine(mode="smart")
    return ApprovalBroker(engine, store=store, **kwargs)


def _persisted(store: SessionStore, session_id: str, approval_id: str) -> dict:
    return store.load_meta(session_id)[f"approval:{approval_id}"]


async def test_allow_and_deny_persisted(tmp_path):
    store = SessionStore(tmp_path)
    store.begin("s1", task="t", model="m", agent="a", workspace="w")
    broker = _broker(store=store)
    allowed, _ = await broker.decide_tool(
        "read_file", {"path": "a.txt"}, family="edit", runtime_id="native", session_id="s1"
    )
    assert allowed is True

    denied, reason = await broker.decide_tool(
        "bash", {"command": "rm -rf /"}, family="terminal", runtime_id="native", session_id="s1"
    )
    assert denied is False
    assert reason is not None
    meta = store.load_meta("s1")
    outcomes = {v["outcome"] for k, v in meta.items() if k.startswith("approval:")}
    assert outcomes == {"allow", "deny"}


async def test_ask_answer_allow_and_deny(tmp_path):
    store = SessionStore(tmp_path)
    store.begin("s1", task="t", model="m", agent="a", workspace="w")
    broker = _broker(store=store)
    pending_decision = asyncio.ensure_future(
        broker.decide_tool(
            "bash", {"command": "sudo ls"}, family="terminal",
            runtime_id="native", session_id="s1", approval_id="a-allow",
        )
    )
    await asyncio.sleep(0.1)
    assert [r.approval_id for r in broker.pending()] == ["a-allow"]
    broker.answer("a-allow", True)
    allowed, _ = await pending_decision
    assert allowed is True
    assert _persisted(store, "s1", "a-allow")["outcome"] == ApprovalOutcome.ALLOW.value

    denied_decision = asyncio.ensure_future(
        broker.decide_acp("terminal", "sudo ls", runtime_id="acp", session_id="s1", approval_id="a-deny")
    )
    await asyncio.sleep(0.1)
    broker.answer("a-deny", False)
    allowed, reason = await denied_decision
    assert allowed is False
    assert "Denied" in reason
    assert _persisted(store, "s1", "a-deny")["outcome"] == ApprovalOutcome.DENY.value

    with pytest.raises(KeyError):
        broker.answer("nope", True)


async def test_timeout_and_disconnect_deny_closed(tmp_path):
    store = SessionStore(tmp_path)
    store.begin("s1", task="t", model="m", agent="a", workspace="w")
    broker = _broker(store=store, timeout_sec=0.1)
    allowed, reason = await broker.decide_acp(
        "approval", "do it", runtime_id="acp", session_id="s1", approval_id="a-timeout"
    )
    assert allowed is False
    assert "timed out" in reason
    assert _persisted(store, "s1", "a-timeout")["outcome"] == ApprovalOutcome.TIMEOUT.value
    assert broker.pending() == []

    broker = _broker(store=store, timeout_sec=5.0)
    pending = asyncio.ensure_future(
        broker.decide_acp(
            "approval", "do it", runtime_id="acp", session_id="s1", approval_id="a-gone"
        )
    )
    await asyncio.sleep(0.1)
    broker.disconnected("a-gone")
    allowed, reason = await pending
    assert allowed is False
    assert "disconnected" in reason
    assert _persisted(store, "s1", "a-gone")["outcome"] == ApprovalOutcome.DISCONNECTED.value
    with pytest.raises(KeyError):
        broker.disconnected("a-gone")


async def test_heartbeat_loss_denies_as_disconnect(tmp_path):
    alive = True
    store = SessionStore(tmp_path)
    store.begin("s1", task="t", model="m", agent="a", workspace="w")
    broker = _broker(store=store, timeout_sec=5.0, alive=lambda: alive)
    pending = asyncio.ensure_future(
        broker.decide_acp(
            "approval", "do it", runtime_id="acp", session_id="s1", approval_id="a-hb"
        )
    )
    await asyncio.sleep(0.1)
    alive = False
    allowed, _ = await pending
    assert allowed is False
    assert _persisted(store, "s1", "a-hb")["outcome"] == ApprovalOutcome.DISCONNECTED.value


async def test_strict_gaps_reported_not_downgraded():
    agent = AgentCapabilities(families=frozenset({"terminal"}), mediated=frozenset())
    gaps = describe_gaps({"terminal": AuthorityPolicy.GARUDA_ONLY}, agent)
    assert len(gaps) == 1
    assert "garuda_only" in gaps[0]
    safe_default_gaps = describe_gaps(
        {"terminal": AuthorityPolicy.AGENT_PREFERRED}, agent
    )
    assert len(safe_default_gaps) == 1
    assert "safe default" in safe_default_gaps[0]
    assert describe_gaps({}, agent) == safe_default_gaps


class _FailingStore:
    """A session store whose audit writes always fail."""

    def update_meta(self, session_id, updates):
        raise OSError("disk is gone")


async def test_audit_failure_denies_without_executing():
    """An allow/deny decision must not execute when its audit is missing."""
    engine = PermissionEngine(mode="yolo")  # ceiling allows everything
    broker = ApprovalBroker(engine, store=_FailingStore(), timeout_sec=5.0)
    allowed, reason = await broker.decide_tool(
        "read_file", {"path": "a.txt"}, family="edit",
        runtime_id="native", session_id="s1",
    )
    assert allowed is False
    assert reason is not None and "audit" in reason
    allowed, reason = await broker.decide_acp(
        "edit", "a.txt", runtime_id="acp", session_id="s1"
    )
    assert allowed is False
    assert reason is not None and "audit" in reason


async def test_answerer_drives_parked_approvals():
    async def _allow(request):
        return True

    engine = PermissionEngine(mode="smart", bash_rules={"ask": [".*"]})
    broker = ApprovalBroker(engine, timeout_sec=5.0)
    broker.set_answerer(_allow)
    allowed, _ = await broker.decide_tool(
        "bash", {"command": "sudo ls"}, family="terminal", runtime_id="native"
    )
    assert allowed is True

    async def _boom(request):
        raise RuntimeError("responder exploded")

    broker.set_answerer(_boom)
    allowed, reason = await broker.decide_tool(
        "bash", {"command": "sudo ls"}, family="terminal", runtime_id="native"
    )
    assert allowed is False
    assert reason is not None and "Denied" in reason


async def test_facade_runs_share_the_broker_path(tmp_path, monkeypatch):
    """`run_agent_task` (CLI headless / SDK / server) routes ASK through the
    session broker: allows execute with an audit trail, denials block the
    side effect and are audited too."""
    monkeypatch.setenv("GARUDA_LEASES_DIR", str(tmp_path / "leases"))
    from garuda.core.events import EventStore
    from garuda.core.loop import DefaultAgent
    from garuda.core.sessions import SessionStore
    from garuda.interfaces.runner import run_agent_task
    from garuda.model.protocol import ModelResponse
    from garuda.model.script_model import ScriptModel
    from garuda.tools import tools_for_names
    from garuda.types import AgentConfig, ToolCall

    async def _answer_allow(action: str) -> bool:
        return True

    def _script():
        return ScriptModel(
            responses=[
                ModelResponse(
                    content=None,
                    tool_calls=[
                        ToolCall(
                            id="1", name="bash",
                            arguments={"command": "touch broker-proof.txt"},
                        )
                    ],
                ),
                ModelResponse(
                    content=None,
                    tool_calls=[
                        ToolCall(id="2", name="task_complete", arguments={"summary": "ok"})
                    ],
                ),
            ]
        )

    def _permissions(handler=None):
        return PermissionEngine(
            mode="smart", bash_rules={"ask": ["touch"]}, approval_handler=handler
        )

    workspace = tmp_path / "ws-allow"
    workspace.mkdir()
    store = SessionStore(tmp_path / "sessions")
    result = await run_agent_task(
        task="touch the proof file",
        model=_script(),
        agent=DefaultAgent(),
        tools=tools_for_names(["bash", "task_complete"]),
        config=AgentConfig(max_turns=5, enable_verifier=False, permission_mode="smart"),
        permissions=_permissions(_answer_allow),
        workspace=str(workspace),
        events=EventStore(session_id="broker-allow"),
        store=store,
    )
    assert result.success
    assert (workspace / "broker-proof.txt").exists()
    meta = store.load_meta("broker-allow")
    assert any(
        v.get("outcome") == ApprovalOutcome.ALLOW.value
        for k, v in meta.items() if k.startswith("approval:")
    ), "the allow decision must be audited"

    workspace2 = tmp_path / "ws-deny"
    workspace2.mkdir()
    result = await run_agent_task(
        task="touch the proof file",
        model=_script(),
        agent=DefaultAgent(),
        tools=tools_for_names(["bash", "task_complete"]),
        config=AgentConfig(max_turns=5, enable_verifier=False, permission_mode="smart"),
        permissions=_permissions(),  # no handler: deny-all answerer, audited
        workspace=str(workspace2),
        events=EventStore(session_id="broker-deny"),
        store=store,
    )
    assert not (workspace2 / "broker-proof.txt").exists()
    meta = store.load_meta("broker-deny")
    assert any(
        v.get("outcome") == ApprovalOutcome.DENY.value
        for k, v in meta.items() if k.startswith("approval:")
    ), "the denial must be audited"
