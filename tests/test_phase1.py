import json
from pathlib import Path

import pytest

from garuda.core.events import EventStore, EventType
from garuda.core.loop import DefaultAgent
from garuda.model.protocol import ModelResponse
from garuda.model.script_model import ScriptModel
from garuda.tools import default_tools
from garuda.types import AgentConfig, ToolCall
from garuda.workspace.local import LocalEnvironment


@pytest.mark.asyncio
async def test_local_environment_execute(tmp_path: Path):
    env = LocalEnvironment(workspace_root=tmp_path)
    result = await env.execute("echo hello")
    assert result.exit_code == 0
    assert "hello" in result.stdout


@pytest.mark.asyncio
async def test_local_environment_read_write(tmp_path: Path):
    env = LocalEnvironment(workspace_root=tmp_path)
    await env.write_file("notes.txt", "garuda")
    content = await env.read_file("notes.txt")
    assert content == "garuda"


@pytest.mark.asyncio
async def test_default_agent_with_script_model(tmp_path: Path):
    env = LocalEnvironment(workspace_root=tmp_path)
    model = ScriptModel(
        responses=[
            ModelResponse(
                content=None,
                tool_calls=[
                    ToolCall(id="1", name="write_file", arguments={"path": "out.txt", "content": "ok"}),
                ],
            ),
            ModelResponse(content="Task complete. Wrote out.txt.", tool_calls=[]),
        ]
    )
    agent = DefaultAgent()
    result = await agent.run(
        task="Write ok to out.txt",
        model=model,
        env=env,
        tools=default_tools(),
        config=AgentConfig(max_turns=5),
    )
    assert result.success
    assert (tmp_path / "out.txt").read_text() == "ok"


def test_event_store_roundtrip(tmp_path: Path):
    store = EventStore(session_id="test-session")
    store.append(EventType.SESSION_START, {"task": "demo"})
    path = tmp_path / "events.jsonl"
    store.save(path)
    loaded = EventStore.load(path)
    assert loaded.session_id == "test-session"
    assert len(loaded.get_all()) == 1
    assert loaded.get_all()[0]["type"] == "session_start"
