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
    assert describe_gaps({"terminal": AuthorityPolicy.AGENT_PREFERRED}, agent) == []
    assert describe_gaps({}, agent) == []
