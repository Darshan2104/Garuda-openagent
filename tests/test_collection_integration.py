import json

import pytest

from garuda.agents.setup import prepare_agent_run
from garuda.core.events import EventStore
from garuda.core.loop import DefaultAgent
from garuda.core.permissions import PermissionEngine
from garuda.model.config import CollectionPolicy
from garuda.model.protocol import ModelResponse
from garuda.model.script_model import ScriptModel
from garuda.tools import DelegateCollectionTool, default_tools
from garuda.types import AgentConfig, ToolCall
from garuda.workspace.local import LocalEnvironment


class RecordingModel(ScriptModel):
    def __init__(self, responses):
        super().__init__(responses)
        self.schemas = []

    async def complete(self, messages, tools=None, temperature=None, max_tokens=None):
        self.schemas.append(list(tools or []))
        return await super().complete(messages, tools, temperature, max_tokens)


@pytest.mark.asyncio
async def test_reasoning_delegates_read_then_performs_edit_itself(tmp_path):
    (tmp_path / "source.txt").write_text("value=42\n")
    collection = RecordingModel(
        [
            ModelResponse(
                content=None,
                tool_calls=[
                    ToolCall(id="read", name="read_file", arguments={"path": "source.txt"})
                ],
            ),
            ModelResponse(
                content=None,
                tool_calls=[
                    ToolCall(
                        id="report",
                        name="submit_collection",
                        arguments={
                            "summary": "value is 42",
                            "findings": ["source.txt contains value=42"],
                            "evidence": [
                                {
                                    "kind": "file",
                                    "location": "source.txt",
                                    "detail": "line 1",
                                }
                            ],
                            "unknowns": [],
                            "buffer_ids": [],
                        },
                    )
                ],
            ),
        ]
    )
    reasoning = RecordingModel(
        [
            ModelResponse(
                content=None,
                tool_calls=[
                    ToolCall(
                        id="delegate",
                        name="delegate_collection",
                        arguments={
                            "objective": "read source value",
                            "questions": ["what value is configured?"],
                            "allowed_paths": ["source.txt"],
                            "handoff": "none",
                        },
                    )
                ],
            ),
            ModelResponse(
                content=None,
                tool_calls=[
                    ToolCall(
                        id="write",
                        name="write_file",
                        arguments={"path": "result.txt", "content": "42\n"},
                    )
                ],
            ),
            ModelResponse(
                content=None,
                tool_calls=[
                    ToolCall(
                        id="done",
                        name="task_complete",
                        arguments={"summary": "wrote the collected value"},
                    )
                ],
            ),
        ]
    )

    result = await DefaultAgent().run(
        task="copy the configured value",
        model=reasoning,
        collection_model=collection,
        collection_policy=CollectionPolicy(enabled=True),
        env=LocalEnvironment(tmp_path),
        tools=default_tools(),
        config=AgentConfig(enable_verifier=False, bootstrap_environment=False),
        events=EventStore(),
        permissions=PermissionEngine(),
    )

    assert result.success
    assert (tmp_path / "result.txt").read_text() == "42\n"
    parent_names = {entry["function"]["name"] for entry in reasoning.schemas[0]}
    child_names = {entry["function"]["name"] for entry in collection.schemas[0]}
    assert "delegate_collection" in parent_names
    assert "write_file" not in child_names
    delegated_result = next(
        message.content for message in result.messages if message.tool_call_id == "delegate"
    )
    assert json.loads(delegated_result)["report"]["summary"] == "value is 42"


@pytest.mark.asyncio
async def test_single_model_run_has_no_collection_tool_or_extra_call(tmp_path):
    reasoning = RecordingModel([ModelResponse(content="done", tool_calls=[])])
    result = await DefaultAgent().run(
        task="answer",
        model=reasoning,
        collection_model=None,
        collection_policy=CollectionPolicy(enabled=True),
        env=LocalEnvironment(tmp_path),
        tools=[*default_tools(), DelegateCollectionTool()],
        config=AgentConfig(enable_verifier=False, bootstrap_environment=False),
    )
    assert result.success
    assert len(reasoning.schemas) == 1
    assert "delegate_collection" not in {
        entry["function"]["name"] for entry in reasoning.schemas[0]
    }


@pytest.mark.asyncio
async def test_shared_setup_enables_tool_and_profile_can_explicitly_disable_it(
    tmp_path, monkeypatch
):
    global_settings = tmp_path / "global.yaml"
    global_settings.write_text("collection:\n  enabled: true\n")
    monkeypatch.setenv("GARUDA_GLOBAL_SETTINGS", str(global_settings))
    reasoning = RecordingModel([ModelResponse(content="done", tool_calls=[])])
    collection = RecordingModel([])
    prepared = await prepare_agent_run(
        "build",
        workspace=str(tmp_path),
        reasoning_model=reasoning,
        collection_model=collection,
    )
    await prepared.agent.run(
        task="answer",
        model=prepared.reasoning,
        collection_model=prepared.collection,
        collection_policy=prepared.collection_policy,
        env=LocalEnvironment(tmp_path),
        tools=prepared.tools,
        config=prepared.config,
        permissions=prepared.permissions,
    )
    assert "delegate_collection" in {
        entry["function"]["name"] for entry in reasoning.schemas[0]
    }

    agents = tmp_path / "agents"
    agents.mkdir()
    (agents / "disabled.yaml").write_text(
        "name: disabled\ntools: [read_file, task_complete]\n"
        "collection:\n  enabled: false\n"
    )
    disabled_reasoning = RecordingModel([ModelResponse(content="done", tool_calls=[])])
    disabled = await prepare_agent_run(
        "disabled",
        workspace=str(tmp_path),
        agents_dir=agents,
        reasoning_model=disabled_reasoning,
        collection_model=collection,
    )
    await disabled.agent.run(
        task="answer",
        model=disabled.reasoning,
        collection_model=disabled.collection,
        collection_policy=disabled.collection_policy,
        env=LocalEnvironment(tmp_path),
        tools=disabled.tools,
        config=disabled.config,
        permissions=disabled.permissions,
    )
    assert "delegate_collection" not in {
        entry["function"]["name"] for entry in disabled_reasoning.schemas[0]
    }
