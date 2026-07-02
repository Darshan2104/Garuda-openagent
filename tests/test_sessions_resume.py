"""Tests for session persistence and resume through the runner."""

import json

import pytest

from garuda.core.events import EventStore
from garuda.core.loop import DefaultAgent
from garuda.core.permissions import PermissionEngine
from garuda.core.sessions import SessionStore
from garuda.interfaces.runner import run_agent_task
from garuda.model.protocol import ModelResponse
from garuda.model.script_model import ScriptModel
from garuda.tools import tools_for_names
from garuda.types import AgentConfig, Role, ToolCall


def _script_model(summary: str) -> ScriptModel:
    return ScriptModel(
        responses=[
            ModelResponse(
                content=None,
                tool_calls=[
                    ToolCall(id="1", name="task_complete", arguments={"summary": summary})
                ],
            )
        ]
    )


async def _run(task: str, summary: str, workspace, events: EventStore, resume=None):
    return await run_agent_task(
        task=task,
        model=_script_model(summary),
        agent=DefaultAgent(),
        tools=tools_for_names(["task_complete"]),
        config=AgentConfig(max_turns=5, enable_verifier=False, permission_mode="yolo"),
        permissions=PermissionEngine(mode="yolo"),
        workspace=str(workspace),
        events=events,
        resume=resume,
    )


@pytest.mark.asyncio
async def test_run_persists_session(tmp_path, monkeypatch):
    monkeypatch.setenv("GARUDA_SESSIONS_DIR", str(tmp_path / "sessions"))
    events = EventStore()
    result = await _run("first task", "Completed the first persistence run.", tmp_path, events)
    assert result.success

    store = SessionStore()
    session_dir = store.session_dir(events.session_id)
    assert session_dir.is_dir()

    meta = json.loads((session_dir / "meta.json").read_text(encoding="utf-8"))
    assert meta["status"] == "success"
    assert meta["task"] == "first task"

    events_lines = (session_dir / "events.jsonl").read_text(encoding="utf-8").strip()
    assert events_lines
    assert all(json.loads(line) for line in events_lines.splitlines())

    messages = store.load_messages(events.session_id)
    assert any(m.role == Role.USER and m.content == "first task" for m in messages)


@pytest.mark.asyncio
async def test_resume_seeds_prior_messages(tmp_path, monkeypatch):
    monkeypatch.setenv("GARUDA_SESSIONS_DIR", str(tmp_path / "sessions"))

    first_events = EventStore()
    first = await _run(
        "first task", "Completed the first persistence run.", tmp_path, first_events
    )
    assert first.success

    second_events = EventStore()
    second = await _run(
        "second task",
        "Completed the resumed follow-up run.",
        tmp_path,
        second_events,
        resume=first_events.session_id[:8],
    )
    assert second.success
    assert second_events.session_id != first_events.session_id

    contents = [m.content for m in second.messages]
    assert "first task" in contents  # prior turn carried into the new context
    assert "second task" in contents

    store = SessionStore()
    meta = store.load_meta(second_events.session_id)
    assert meta["resumed_from"] == first_events.session_id
    assert meta["status"] == "success"


@pytest.mark.asyncio
async def test_resume_latest_resolves_most_recent(tmp_path, monkeypatch):
    monkeypatch.setenv("GARUDA_SESSIONS_DIR", str(tmp_path / "sessions"))

    first_events = EventStore()
    await _run("first task", "Completed the first persistence run.", tmp_path, first_events)

    second_events = EventStore()
    second = await _run(
        "second task",
        "Completed the resumed follow-up run.",
        tmp_path,
        second_events,
        resume="latest",
    )
    assert second.success
    meta = SessionStore().load_meta(second_events.session_id)
    assert meta["resumed_from"] == first_events.session_id


@pytest.mark.asyncio
async def test_resume_unknown_session_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("GARUDA_SESSIONS_DIR", str(tmp_path / "sessions"))
    with pytest.raises(FileNotFoundError):
        await _run("task", "Never runs.", tmp_path, EventStore(), resume="deadbeef")
