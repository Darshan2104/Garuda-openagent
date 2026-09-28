import hashlib
import json

import pytest

from garuda.context.manager import ContextManager
from garuda.core.buffer import ToolOutputBuffer
from garuda.core.collection import CollectionCoordinator
from garuda.core.events import SUBAGENT_LOG_DIR, EventStore
from garuda.core.permissions import PermissionEngine
from garuda.model.config import CollectionPolicy
from garuda.model.protocol import ModelResponse
from garuda.model.script_model import ScriptModel
from garuda.tools import default_tools
from garuda.types import Message, Role, ToolCall
from garuda.workspace.local import LocalEnvironment


class RecordingModel(ScriptModel):
    def __init__(self, responses):
        super().__init__(responses)
        self.requests = []

    async def complete(self, messages, tools=None, temperature=None, max_tokens=None):
        self.requests.append((list(messages), list(tools or []), max_tokens))
        return await super().complete(messages, tools, temperature, max_tokens)


def _parent_context():
    parent = ScriptModel([])
    context = ContextManager(model=parent, task="parent task")
    context.seed(
        [
            Message(role=Role.SYSTEM, content="parent system"),
            Message(role=Role.USER, content="RAW_TRANSCRIPT_SECRET"),
        ]
    )
    context.set_state_provider(lambda: "## Goal\nInspect source.txt\n## Files\nsource.txt")
    return context


@pytest.mark.asyncio
async def test_coordinator_uses_separate_context_and_joinable_child_trace(tmp_path):
    (tmp_path / "source.txt").write_text("value=42\n")
    model = RecordingModel(
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
                        id="submit",
                        name="submit_collection",
                        arguments={
                            "summary": "The value is 42.",
                            "findings": ["source.txt defines value=42"],
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
    events = EventStore()
    events.attach_persistence(tmp_path / "session" / "events.jsonl")
    coordinator = CollectionCoordinator(
        model=model,
        policy=CollectionPolicy(enabled=True, handoff="brief"),
        env=LocalEnvironment(tmp_path),
        events=events,
        parent_context=_parent_context(),
        parent_buffer=None,
        base_tools=default_tools(),
        parent_permissions=PermissionEngine(mode="smart"),
        parent_task="parent objective",
    )

    payload = json.loads(
        await coordinator.delegate(
            {
                "objective": "read the configured value",
                "questions": ["what is value?"],
                "allowed_paths": ["source.txt"],
                "handoff": "brief",
            }
        )
    )

    assert payload["report"]["findings"] == ["source.txt defines value=42"]
    assert payload["attempt"]["model"] == model.model_name
    child_id = payload["attempt"]["session_id"]
    assert (tmp_path / "session" / SUBAGENT_LOG_DIR / f"{child_id}.jsonl").is_file()
    joined = [e for e in events.get_all() if e["type"] == "collection"][-1]["payload"]
    assert joined["child_session_id"] == child_id
    first_messages, first_tools, max_tokens = model.requests[0]
    rendered = "\n".join(message.content for message in first_messages)
    assert "## Goal" in rendered
    assert "parent objective" in rendered
    assert "RAW_TRANSCRIPT_SECRET" not in rendered
    assert max_tokens == coordinator.policy.budget.max_tokens_per_job
    assert {entry["function"]["name"] for entry in first_tools} == {
        "read_file",
        "grep",
        "glob",
        "ls",
        "buffer_grep",
        "buffer_slice",
        "buffer_query",
        "read_pdf",
        "read_spreadsheet",
        "image_read",
        "submit_collection",
    }


@pytest.mark.asyncio
async def test_prose_only_worker_cannot_complete_collection(tmp_path):
    model = RecordingModel([ModelResponse(content="I found it", tool_calls=[])])
    coordinator = CollectionCoordinator(
        model=model,
        policy=CollectionPolicy(enabled=True),
        env=LocalEnvironment(tmp_path),
        events=EventStore(),
        parent_context=_parent_context(),
        parent_buffer=None,
        base_tools=default_tools(),
        parent_permissions=PermissionEngine(),
    )
    with pytest.raises(ValueError, match="did not submit"):
        await coordinator.delegate(
            {"objective": "inspect", "questions": ["what?"], "max_turns": 1}
        )


@pytest.mark.asyncio
async def test_child_reads_remain_under_parent_path_permission_ceiling(tmp_path):
    (tmp_path / "secret.txt").write_text("do-not-read")
    model = RecordingModel(
        [
            ModelResponse(
                content=None,
                tool_calls=[
                    ToolCall(id="read", name="read_file", arguments={"path": "secret.txt"})
                ],
            ),
            ModelResponse(
                content=None,
                tool_calls=[
                    ToolCall(
                        id="submit",
                        name="submit_collection",
                        arguments={
                            "summary": "The parent policy blocked the requested read.",
                            "findings": [],
                            "evidence": [],
                            "unknowns": ["The file contents are unknown."],
                            "buffer_ids": [],
                        },
                    )
                ],
            ),
        ]
    )
    coordinator = CollectionCoordinator(
        model=model,
        policy=CollectionPolicy(enabled=True),
        env=LocalEnvironment(tmp_path),
        events=EventStore(),
        parent_context=_parent_context(),
        parent_buffer=None,
        base_tools=default_tools(),
        parent_permissions=PermissionEngine(path_rules={"deny": ["secret.txt"]}),
    )
    payload = json.loads(
        await coordinator.delegate(
            {"objective": "inspect", "questions": ["what is secret?"]}
        )
    )
    assert payload["report"]["unknowns"]
    second_messages = model.requests[1][0]
    denial = next(message for message in second_messages if message.tool_call_id == "read")
    assert "Permission denied" in denial.content


@pytest.mark.asyncio
async def test_child_created_large_output_stays_retrievable_and_reportable(tmp_path):
    (tmp_path / "large.txt").write_text("\n".join(f"line-{i}" for i in range(100)))
    buffer_id = "buf_" + hashlib.sha1(b"read-large").hexdigest()[:10]
    model = RecordingModel(
        [
            ModelResponse(
                content=None,
                tool_calls=[
                    ToolCall(
                        id="read-large", name="read_file", arguments={"path": "large.txt"}
                    )
                ],
            ),
            ModelResponse(
                content=None,
                tool_calls=[
                    ToolCall(
                        id="slice",
                        name="buffer_slice",
                        arguments={"buffer_id": buffer_id, "start_line": 90, "end_line": 92},
                    )
                ],
            ),
            ModelResponse(
                content=None,
                tool_calls=[
                    ToolCall(
                        id="submit",
                        name="submit_collection",
                        arguments={
                            "summary": "The large read was preserved.",
                            "findings": ["Lines 90-92 are retrievable."],
                            "evidence": [
                                {
                                    "kind": "buffer",
                                    "location": buffer_id,
                                    "detail": "lines 90-92",
                                }
                            ],
                            "unknowns": [],
                            "buffer_ids": [buffer_id],
                        },
                    )
                ],
            ),
        ]
    )
    parent_buffer = ToolOutputBuffer(
        "parent", threshold_bytes=32, root=tmp_path / "buffers"
    )
    coordinator = CollectionCoordinator(
        model=model,
        policy=CollectionPolicy(enabled=True),
        env=LocalEnvironment(tmp_path),
        events=EventStore(),
        parent_context=_parent_context(),
        parent_buffer=parent_buffer,
        base_tools=default_tools(),
        parent_permissions=PermissionEngine(),
    )
    payload = json.loads(
        await coordinator.delegate(
            {
                "objective": "inspect the large file",
                "questions": ["can the tail be retrieved?"],
                "allowed_paths": ["large.txt"],
            }
        )
    )
    assert payload["report"]["buffer_ids"] == [buffer_id]
    assert "line-89" in model.requests[2][0][-1].content
