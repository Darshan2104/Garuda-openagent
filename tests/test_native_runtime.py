"""Native bridge tests for issue #14 (P0.7).

A prompt through `NativeGarudaRuntime` produces the same task result as the
direct loop; the shared conformance suite passes against it; and the
`run_agent_task` facade — the single path behind CLI, SDK, server, and web —
records unified sessions.
"""

import pytest

from garuda.core.events import EventStore
from garuda.core.loop import DefaultAgent
from garuda.core.permissions import PermissionEngine
from garuda.core.sessions import SessionStore
from garuda.interfaces.runner import run_agent_task
from garuda.model.protocol import ModelResponse
from garuda.model.script_model import ScriptModel
from garuda.runtime import (
    AgentRuntime,
    LifecycleState,
    RuntimeClosedError,
    RuntimeProtocolError,
    RuntimeStartError,
)
from garuda.runtime.native import NativeGarudaRuntime
from garuda.tools import tools_for_names
from garuda.types import AgentConfig, ToolCall
from garuda.workspace.local import LocalEnvironment
from tests.test_runtime_conformance import run_conformance_suite


def _script_model(summary: str) -> ScriptModel:    return ScriptModel(
        responses=[
            ModelResponse(
                content=None,
                tool_calls=[
                    ToolCall(id="1", name="task_complete", arguments={"summary": summary})
                ],
            )
        ]
    )


async def _noop_driver(*, task: str, turn: int, trail: EventStore):
    return None


def _parts(tmp_path, summary: str, store=None):
    model = _script_model(summary)
    agent = DefaultAgent()
    tools = tools_for_names(["task_complete"])
    config = AgentConfig(max_turns=5, enable_verifier=False, permission_mode="yolo")
    permissions = PermissionEngine(mode="yolo")
    store = store or SessionStore(tmp_path / "sessions")
    return model, agent, tools, config, permissions, store


def _native(tmp_path, summary: str, store=None, **kwargs) -> NativeGarudaRuntime:
    model, agent, tools, config, permissions, store = _parts(tmp_path, summary, store)

    async def _driver(*, task: str, turn: int, trail: EventStore):
        env = LocalEnvironment(workspace_root=tmp_path)
        return await agent.run(
            task=task,
            model=model,
            env=env,
            tools=tools,
            config=config,
            events=trail,
            permissions=permissions,
        )

    return NativeGarudaRuntime(
        agent=agent,
        model=model,
        tools=tools,
        config=config,
        permissions=permissions,
        store=store,
        run=_driver,
        **kwargs,
    )


async def test_prompt_matches_the_direct_loop(tmp_path):
    runtime = _native(tmp_path, "Bridged run.")
    await runtime.start(task="bridge task", session_id="n1")
    assert isinstance(runtime, AgentRuntime)
    turn = await runtime.prompt("bridge task")
    assert turn == 1
    assert runtime.last_result is not None
    assert runtime.last_result.success
    assert runtime.last_result.final_message == "Bridged run."

    model, agent, tools, config, permissions, _ = _parts(tmp_path, "Bridged run.")
    direct = await agent.run(
        task="bridge task",
        model=model,
        env=LocalEnvironment(workspace_root=tmp_path),
        tools=tools,
        config=config,
        events=EventStore(),
        permissions=permissions,
    )
    assert direct.success == runtime.last_result.success
    assert direct.final_message == runtime.last_result.final_message
    await runtime.close()


async def test_native_runtime_passes_the_shared_conformance_suite(tmp_path):
    await run_conformance_suite(lambda: _native(tmp_path, "Conformance run."))


async def test_facade_records_unified_sessions(tmp_path):
    """Every entry point funnels through `run_agent_task`; one assertion here
    covers CLI, SDK, server, and web at once."""
    store = SessionStore(tmp_path / "sessions")
    events = EventStore()
    result = await run_agent_task(
        task="facade task",
        model=_script_model("Facade run."),
        agent=DefaultAgent(),
        tools=tools_for_names(["task_complete"]),
        config=AgentConfig(max_turns=5, enable_verifier=False, permission_mode="yolo"),
        permissions=PermissionEngine(mode="yolo"),
        workspace=str(tmp_path),
        events=events,
        store=store,
    )
    assert result.success
    unified = store.load_unified(events.session_id)
    assert unified.active.runtime_id == "native"
    assert unified.active.native_session_id == events.session_id


async def test_resume_reattaches_with_replayable_history(tmp_path):
    first = _native(tmp_path, "First run.")
    await first.start(task="resume task", session_id="r1")
    await first.prompt("resume task")
    await first.close()

    store = SessionStore(tmp_path / "sessions")
    second = _native(tmp_path, "Second run.", store=store)
    info = await second.resume(native_session_id="r1")
    assert info.native_session_id == "r1"
    events, _ = await second.poll_events(0)
    assert events, "resumed runtime replays its trail"
    await second.close()
    assert second.state is LifecycleState.CLOSED


async def test_cancel_permission_and_driver_edges(tmp_path):
    runtime = _native(tmp_path, "Never runs.")
    await runtime.start(task="edge task", session_id="e1")
    await runtime.cancel(reason="changed mind")
    assert runtime.state is LifecycleState.CLOSED
    with pytest.raises(RuntimeClosedError):
        await runtime.prompt("too late")

    runtime = _native(tmp_path, "Needs approval.")
    await runtime.start(task="approval task", session_id="e2")
    answered: list[bool] = []
    runtime.park_approval("apr-1", answered.append)
    await runtime.permission_response(approval_id="apr-1", allow=True)
    assert answered == [True]
    with pytest.raises(RuntimeProtocolError):
        await runtime.permission_response(approval_id="apr-1", allow=True)

    bare = NativeGarudaRuntime(
        agent=DefaultAgent(),
        model=_script_model("x"),
        tools=[],
        config=AgentConfig(),
        permissions=PermissionEngine(mode="yolo"),
        store=SessionStore(tmp_path / "bare"),
    )
    await bare.start(task="no driver", session_id="e3")
    with pytest.raises(RuntimeStartError, match="no run driver"):
        await bare.prompt("go")
    bare.install_driver(_noop_driver)
    with pytest.raises(RuntimeStartError, match="already installed"):
        bare.install_driver(_noop_driver)
