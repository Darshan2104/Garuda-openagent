import hashlib
import json

import pytest

from garuda.context.manager import ContextManager
from garuda.core.buffer import ToolOutputBuffer
from garuda.core.collection import CollectionCoordinator
from garuda.core.events import SUBAGENT_LOG_DIR, EventStore
from garuda.core.permissions import PermissionEngine
from garuda.model.config import CollectionBudget, CollectionPolicy
from garuda.model.protocol import ModelResponse
from garuda.model.script_model import ScriptModel
from garuda.tools import default_tools
from garuda.types import Message, Role, ToolCall
from garuda.workspace.local import LocalEnvironment


@pytest.mark.parametrize("capture_kind", ["legacy", "buffer", "inline", "failed", "foreign", "child-inline", "child-referenced", "child-malformed", "metadata-container", "invalid-inline", "invalid-failed", "invalid-foreign", "null-capture", "character-frame", "receipt-error", "runner-exception", "missing-arguments", "unknown-tool", "workspace-refusal", "screen-denial", "hook-unknown", "argument-parse"])
async def test_brief_collection_grants_only_owned_buffer_references(tmp_path, capture_kind):
    from garuda.core.artifact_read_model import failed_descriptor, inline_descriptor, source_capture

    owner = ToolOutputBuffer("parent", root=tmp_path / "buffers")
    retained = owner.store("parent-secret", "PARENT_PRIVATE", tool_name="bash")
    parent = _parent_context()
    parent.attach_buffer(owner)
    content = "Literal [buffer:parent-secret] in an observation."
    metadata = {}
    runner_delivery = None
    source = source_capture(kind="tool_observation", completeness="complete")
    if capture_kind == "buffer":
        metadata["artifact_capture"] = retained.artifact
    elif capture_kind == "argument-parse":
        from garuda.core.loop import DefaultAgent
        from garuda.model.litellm_model import TOOL_ARG_PARSE_ERROR_KEY
        from garuda.types import AgentConfig

        native = ScriptModel([
            ModelResponse(content=None, tool_calls=[ToolCall(
                id="parent-read", name="buffer_slice", arguments={TOOL_ARG_PARSE_ERROR_KEY: content},
            )]),
            ModelResponse(content=None, tool_calls=[ToolCall(
                id="finish", name="task_complete", arguments={"summary": "Completed the native feedback check."},
            )]),
        ])
        result = await DefaultAgent().run(
            task="Exercise native feedback", model=native, env=LocalEnvironment(tmp_path),
            tools=default_tools(), config=AgentConfig(enable_verifier=False, max_turns=3),
            events=EventStore(session_id="parent"), permissions=PermissionEngine(mode="yolo"),
            context=parent, buffer=owner,
        )
        assert result.success
        runner_delivery = next(message for message in parent.get_messages() if message.tool_call_id == "parent-read")
    elif capture_kind in {"character-frame", "receipt-error", "runner-exception", "missing-arguments", "unknown-tool", "workspace-refusal", "screen-denial", "hook-unknown"}:
        from garuda.core.tool_runner import ToolRunner
        from garuda.plugins.hooks import HookRegistry
        from garuda.tools.protocol import ToolContext

        owner.store("observation", content)
        owner.threshold_bytes = 1  # An exact frame/error must bypass rebuffering.
        arguments = {"buffer_id": "observation", "start_char": 0, "end_char": len(content)}
        tool_name = "buffer_slice"
        if capture_kind == "receipt-error":
            arguments["child_result_reference"] = None
        elif capture_kind == "runner-exception":
            arguments = {"buffer_id": "observation", "start_line": "[buffer:parent-secret]", "end_line": 1}
        elif capture_kind == "missing-arguments":
            arguments = {"[buffer:parent-secret]": "mistyped argument"}
        elif capture_kind in {"unknown-tool", "screen-denial"}:
            tool_name, arguments = "[buffer:parent-secret]", {}
        elif capture_kind == "workspace-refusal":
            tool_name = "read_file"
            arguments = {"path": str(tmp_path.parent / (tmp_path.name + "-[buffer:parent-secret].txt"))}
        tools = default_tools()
        permissions = PermissionEngine(mode="yolo", tool_rules={tool_name: "deny"}) \
            if capture_kind == "screen-denial" else PermissionEngine(mode="yolo")
        hooks = HookRegistry()
        if capture_kind == "hook-unknown":
            async def rewrite(call, _context):
                return ToolCall(id=call.id, name="[buffer:parent-secret]", arguments=call.arguments)

            hooks.register_before_tool(rewrite)
        runner = ToolRunner(
            tool_map={tool.name: tool for tool in tools}, env=LocalEnvironment(tmp_path),
            ctx=ToolContext(session_id="parent", buffer=owner, output_budget=parent.output_budget),
            context=parent, permissions=permissions, hooks=hooks,
            events=EventStore(session_id="parent"),
        )
        call, denied = await runner.screen(ToolCall(
            id="parent-read", name=tool_name, arguments=arguments,
        ), turn=1)
        if capture_kind in {"screen-denial", "hook-unknown"}:
            assert call is None and denied is not None
            parent.append(denied)
            runner_delivery = denied
        else:
            assert call is not None and denied is None
            runner_delivery = await runner.run_one(call, turn=1)
        if capture_kind in {"character-frame", "receipt-error"}:
            receipt_key = "buffer_slice_frame" if capture_kind == "character-frame" else "buffer_slice_receipt_error"
            receipt = runner_delivery.metadata[receipt_key]
            assert receipt["content_sha256"] == hashlib.sha256(runner_delivery.content.encode()).hexdigest()
        if capture_kind not in {"screen-denial", "hook-unknown"}:
            assert runner_delivery.is_error is (capture_kind != "character-frame")
        if capture_kind == "workspace-refusal":
            assert runner_delivery.metadata["permission_denied"] is True
        assert len(runner_delivery.content.encode()) <= parent.output_budget(capture_kind != "character-frame")
    elif capture_kind in {"inline", "invalid-inline"}:
        metadata["artifact_capture"] = inline_descriptor(content, storage_session_id="parent", producer="bash", source=source)
    elif capture_kind in {"failed", "invalid-failed"}:
        metadata["artifact_capture"] = failed_descriptor(storage_session_id="parent", producer="bash", source=source, reason="capture_failed")
    elif capture_kind in {"foreign", "invalid-foreign"}:
        foreign = ToolOutputBuffer("foreign", root=tmp_path / "foreign" / "buffers")
        metadata["artifact_capture"] = foreign.store("parent-secret", "FOREIGN", tool_name="bash").artifact
    elif capture_kind.startswith("child-"):
        from garuda.core.child_result import (
            accepted_output_contract,
            build_child_result_envelope,
            render_child_result_handoff,
        )
        from garuda.types import AgentResult

        value = content * 1000 if capture_kind == "child-referenced" else content
        contract = accepted_output_contract({"type": "string"}, value)
        contract.update(agent_name="typed", agent_digest="typed-digest")
        result = AgentResult(success=True, final_message="Child complete.", turns=1, messages=[], output=value, metadata={
            "output_contract": contract, "subagent_session_id": "typed-child", "subagent_profile": "typed",
            "subagent_resolved_profile": "typed",
        })
        child = build_child_result_envelope(result, "typed", storage=owner, parent_tools=default_tools(), output_budget=30720)
        if capture_kind == "child-malformed":
            child["outcome"]["validation"] = []
        metadata["child_result"] = child
        content = render_child_result_handoff(content, child, 30720)
    elif capture_kind == "metadata-container":
        metadata = []
    elif capture_kind == "null-capture":
        metadata["artifact_capture"] = None
    if capture_kind.startswith("invalid-"):
        metadata["artifact_capture"]["version"] = 99
    if runner_delivery is None:
        parent.append(Message(role=Role.TOOL, content=content, name="bash", tool_call_id="parent-read", metadata=metadata))
    model = RecordingModel([
        ModelResponse(content=None, tool_calls=[ToolCall(id="slice", name="buffer_slice", arguments={
            "buffer_id": "parent-secret", "start_line": 1, "end_line": 1,
        })]),
        ModelResponse(content=None, tool_calls=[ToolCall(id="submit", name="submit_collection", arguments={
            "summary": "Completed the scoped buffer visibility check.", "findings": [], "evidence": [],
            "unknowns": ["No reportable findings were supplied."], "buffer_ids": [],
        })]),
    ])
    coordinator = CollectionCoordinator(model=model, policy=CollectionPolicy(enabled=True),
        env=LocalEnvironment(tmp_path), events=EventStore(), parent_context=parent,
        parent_buffer=owner, base_tools=default_tools(), parent_permissions=PermissionEngine())
    result = json.loads(await coordinator.delegate({"objective": "check scoped access", "questions": ["which buffers are visible?"]}))
    assert "report" in result
    delivered = next(message for message in model.requests[1][0] if message.tool_call_id == "slice")
    assert ("PARENT_PRIVATE" in delivered.content) is (capture_kind in {"legacy", "buffer"})
    brief = parent.fork(mode="brief").get_messages()[-1].content
    assert ("buffer:parent-secret" in brief) is (capture_kind in {"legacy", "buffer"})
    if runner_delivery is not None:
        capture = runner_delivery.metadata["artifact_capture"]
        if capture_kind == "workspace-refusal":
            assert capture["owner"]["kind"] == "buffer"
            captured = owner.read(capture["owner"]["reference"])
        else:
            assert capture["owner"]["kind"] == "inline-tool-result"
            captured = runner_delivery.content
        assert capture["digest"]["sha256"] == hashlib.sha256(captured.encode()).hexdigest()
        persisted = next(message for message in parent.get_messages() if message.tool_call_id == "parent-read")
        assert persisted.content == runner_delivery.content
        assert persisted.metadata["artifact_capture"] == capture


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
async def test_collection_forces_terminal_submission_after_evidence_turn(tmp_path):
    """A bounded worker gets one submit-only turn after collecting evidence."""
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
                                {"kind": "file", "location": "source.txt", "detail": "line 1"}
                            ],
                            "unknowns": [],
                            "buffer_ids": [],
                        },
                    )
                ],
            ),
        ]
    )
    coordinator = CollectionCoordinator(
        model=model,
        policy=CollectionPolicy(
            enabled=True,
            budget=CollectionBudget(max_turns_per_job=1),
        ),
        env=LocalEnvironment(tmp_path),
        events=EventStore(),
        parent_context=_parent_context(),
        parent_buffer=None,
        base_tools=default_tools(),
        parent_permissions=PermissionEngine(),
    )

    payload = json.loads(
        await coordinator.delegate(
            {"objective": "read source", "questions": ["what is the value?"]}
        )
    )

    assert payload["report"]["findings"] == ["source.txt defines value=42"]
    forced_tools = {entry["function"]["name"] for entry in model.requests[-1][1]}
    assert forced_tools == {"submit_collection"}


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
