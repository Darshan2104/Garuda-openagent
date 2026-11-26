"""A plan-mode run keeps plan mode and returns its plan (#771).

claude-agent-acp ends planning with an "Approve Plan" ``switch_mode``
permission request (ExitPlanMode) that carries the plan. Garuda refuses it
itself, whatever the approval handler would say, and the turn the agent then
ends ``cancelled`` completes with the plan as its reply.
"""

import asyncio
import sys

import pytest

from garuda.acp.adapter import AcpRuntime, _plan_text
from garuda.acp.fake_agent import FAKE_PLAN
from garuda.acp.protocol import AcpCancelledError
from garuda.runtime import LifecycleState

FAKE = [sys.executable, "-m", "garuda.acp.fake_agent", "--profile", "plan-exit"]


def _runtime(mode: str, handler) -> AcpRuntime:
    return AcpRuntime([*FAKE, "--initial-mode", mode], runtime_id="fake-plan-exit",
                      approval_handler=handler)


def _reply(events) -> str:
    return "".join(e.payload.get("chunk", "") for e in events if e.kind.value == "message")


async def _allow_everything(action: str) -> bool:
    raise AssertionError(f"a plan-mode exit reached the approval handler: {action}")


async def test_plan_mode_exit_is_refused_and_the_plan_is_the_reply():
    runtime = _runtime("plan", _allow_everything)
    await runtime.start(task="t", session_id="p1")
    await asyncio.wait_for(runtime.prompt("plan the change"), 15)
    assert runtime.state is LifecycleState.IDLE
    events, _ = await runtime.poll_events(0)
    assert _reply(events) == FAKE_PLAN
    assert "left plan mode" not in _reply(events)  # the reject option, never an allow
    [settled] = [e for e in events if e.payload.get("state") == "approval_settled"]
    assert (settled.payload["tool_kind"], settled.payload["outcome"]) == ("switch_mode", "deny")
    [produced] = [e for e in events if e.payload.get("state") == "plan_produced"]
    assert produced.payload["chars"] == len(FAKE_PLAN)
    states = [e.payload.get("state") for e in events if e.kind.value == "lifecycle"]
    assert "completed" in states and "cancelled" not in states
    assert runtime.reported_mode == "plan"

    # The session stays in plan mode and takes another turn the same way.
    await asyncio.wait_for(runtime.prompt("plan it again"), 15)
    later, _ = await runtime.poll_events(len(events))
    assert _reply(later) == FAKE_PLAN
    await runtime.close()


async def test_plan_mode_exit_without_a_plan_is_still_refused():
    runtime = _runtime("plan", _allow_everything)
    await runtime.start(task="t", session_id="p2")
    with pytest.raises(AcpCancelledError) as caught:
        await asyncio.wait_for(runtime.prompt("no plan"), 15)
    assert "denied" not in str(caught.value)
    events, _ = await runtime.poll_events(0)
    assert "left plan mode" not in _reply(events)
    assert runtime.state is LifecycleState.CLOSED


async def test_switch_mode_outside_plan_mode_is_left_to_the_handler():
    asked: list[str] = []

    async def allow(action: str) -> bool:
        asked.append(action)
        return True

    runtime = _runtime("ask", allow)
    await runtime.start(task="t", session_id="p3")
    await asyncio.wait_for(runtime.prompt("plan the change"), 15)
    [action] = asked
    assert "kind=switch_mode" in action
    events, _ = await runtime.poll_events(0)
    assert _reply(events).startswith("left plan mode (default)")
    assert not [e for e in events if e.payload.get("state") == "plan_produced"]
    await runtime.close()


def test_plan_text_prefers_raw_input_then_content():
    assert _plan_text({"rawInput": {"plan": "  do it  "}}) == "do it"
    content = [{"type": "content", "content": {"type": "text", "text": "step 1"}},
               {"type": "content", "content": {"type": "text", "text": "step 2"}}]
    assert _plan_text({"rawInput": {}, "content": content}) == "step 1\n\nstep 2"
    assert _plan_text({"rawInput": {"plan": ""}}) is None
    assert _plan_text({}) is None
