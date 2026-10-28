"""Final-output JSON Schema (#165, plan task H.12b)."""

import hashlib
import json
import subprocess
from copy import deepcopy

import pytest

from garuda.agents.loader import load_profile
from garuda.agents.setup import resolve_profile
from garuda.core.buffer import ToolOutputBuffer
from garuda.core.child_result import (
    build_child_result_envelope,
    canonical_json,
    render_child_result_handoff,
    schema_identity,
    validate_child_result_envelope,
)
from garuda.core.loop import DefaultAgent
from garuda.core.sessions import message_from_dict, message_to_dict
from garuda.model.config import ConfigError
from garuda.model.protocol import ModelResponse
from garuda.model.script_model import ScriptModel
from garuda.tools import default_tools, tools_for_names
from garuda.types import AgentConfig, Role, ToolCall
from garuda.workspace.local import LocalEnvironment

SCHEMA = {
    "type": "object",
    "properties": {"count": {"type": "integer"}, "tags": {"type": "array", "items": {"$ref": "#/$defs/tag"}}},
    "required": ["count"],
    "additionalProperties": False,
    "$defs": {"tag": {"type": "string", "minLength": 1}},
}
SUMMARY = "A fully detailed completion summary of the work done."
_OMITTED = object()


def _typed_agent(tmp_path, schema, *, extends="garuda/explore", extra=""):
    agents = tmp_path / "agents"
    (agents / "schemas").mkdir(parents=True)
    (agents / "schemas" / "result.json").write_text(json.dumps(schema))
    (agents / "typed.yaml").write_text(
        f"version: 1\nextends: {extends}\n{extra}output: {{schema: schemas/result.json}}\n"
    )
    return agents


def _done(result=_OMITTED, call_id="d", **extra):
    arguments = {"summary": SUMMARY, **extra}
    if result is not _OMITTED:
        arguments["result"] = result
    return ModelResponse(content=None, tool_calls=[ToolCall(id=call_id, name="task_complete",
                                                            arguments=arguments)])


class Recording(ScriptModel):
    """A ScriptModel that remembers the tools offered on each request."""

    def __init__(self, script):
        super().__init__(script)
        self.calls = []

    async def complete(self, messages, tools=None, *a, **k):
        self.calls.append(tools)
        return await super().complete(messages, tools, *a, **k)

    async def stream(self, messages, tools=None, *a, **k):
        self.calls.append(tools)
        async for delta in super().stream(messages, tools, *a, **k):
            yield delta


class ConversationRecording(ScriptModel):
    """A ScriptModel that snapshots each native parent/child request."""

    def __init__(self, script):
        super().__init__(script)
        self.requests = []

    async def complete(self, messages, tools=None, *args, **kwargs):
        self.requests.append(deepcopy(messages))
        return await super().complete(messages, tools, *args, **kwargs)


class ReceiptRetrievingModel:
    """Script a real child, then consume its reference through parent turns."""

    def __init__(self, accepted, *, tamper=None, storage=None):
        self.accepted = accepted
        self.tamper = tamper
        self.storage = storage
        self.requests = []
        self.rebuilt = ""
        self.parent_calls = 0
        self.invoked = False

    @property
    def model_name(self):
        return "script/receipt-retrieval"

    @property
    def supports_tool_calling(self):
        return True

    def count_tokens(self, messages):
        return sum(len(message.content) for message in messages) // 4

    async def complete(self, messages, tools=None, *args, **kwargs):
        self.requests.append(deepcopy(messages))
        is_child = any(
            message.role == Role.USER and message.content.startswith("[subagent:typed]")
            for message in messages
        )
        if is_child or self.invoked and not any(
            message.role == Role.TOOL and message.name == "invoke_subagent"
            for message in messages
        ):
            return _done(self.accepted, "child-done")
        handoff = next(
            (message for message in messages
             if message.role == Role.TOOL and message.name == "invoke_subagent"),
            None,
        )
        if handoff is None:
            self.invoked = True
            return ModelResponse(content=None, tool_calls=[ToolCall(
                id="sub", name="invoke_subagent",
                arguments={"profile": "typed", "task": "return the result", "handoff": "none"},
            )])
        slices = [
            message for message in messages
            if message.role == Role.TOOL and message.name == "buffer_slice"
        ]
        if slices:
            latest = slices[-1]
            if "child-result retrieval failed" in latest.content:
                return _done(call_id="parent-done")
            assert not latest.content.startswith("[buffer:"), latest.content
            header, chunk = latest.content.split("\n--- content ---\n", 1)
            self.rebuilt += chunk
            if "complete]" in header:
                return _done(call_id="parent-done")
            start = int(header.split("next_start=", 1)[1].split("]", 1)[0])
        else:
            start = 0
        assert "Validated child result:\n" in handoff.content, (
            handoff.content,
            handoff.metadata.get("child_result"),
        )
        envelope = json.loads(handoff.content.split("Validated child result:\n", 1)[1])
        reference = deepcopy(envelope["delivery"]["reference"])
        if self.tamper == "digest":
            reference["sha256"] = "sha256:" + "0" * 64
        elif self.tamper == "version":
            reference["version"] = True
        elif self.tamper == "format":
            reference["format"] = "text/plain"
        elif self.tamper == "retrieval":
            reference["retrieval"]["mode"] = "lines"
        elif self.tamper == "size":
            reference["serialized_bytes"] += 1
        elif self.tamper == "size_type":
            reference["serialized_bytes"] = True
        elif self.tamper == "parent":
            reference["parent_session_id"] = "another-parent"
        elif self.tamper == "producer":
            reference["producer_session_id"] = "another-child"
        elif self.tamper == "producer_prefix":
            reference["producer_session_id"] = reference["producer_session_id"].split("-", 1)[0]
        elif self.tamper == "buffer":
            reference["buffer_id"] += "-missing"
        elif self.tamper == "malformed":
            reference["unexpected"] = True
        elif self.tamper == "content":
            original = self.storage.read(reference["buffer_id"])
            replacement = original.replace("x", "y", 1)
            assert len(replacement.encode("utf-8")) == len(original.encode("utf-8"))
            self.storage.store(reference["buffer_id"], replacement, tool_name="invoke_subagent")
        elif self.tamper == "noncanonical":
            original = self.storage.read(reference["buffer_id"])
            replacement = original.replace(":", ": ", 1)
            encoded = replacement.encode("utf-8")
            reference["serialized_bytes"] = len(encoded)
            reference["sha256"] = "sha256:" + hashlib.sha256(encoded).hexdigest()
            self.storage.store(reference["buffer_id"], replacement, tool_name="invoke_subagent")
        self.parent_calls += 1
        arguments = {
            "buffer_id": reference["buffer_id"],
            "start_char": start,
            "end_char": start + 8192,
            "child_result_reference": reference,
        }
        if self.tamper == "line_mode":
            arguments.pop("start_char")
            arguments.pop("end_char")
            arguments.update(start_line=1, end_line=1)
        elif self.tamper == "null_reference":
            arguments["child_result_reference"] = None
        return ModelResponse(content=None, tool_calls=[ToolCall(
            id=f"slice-{self.parent_calls}",
            name="buffer_slice",
            arguments=arguments,
        )])

    async def stream(self, messages, tools=None, *args, **kwargs):
        response = await self.complete(messages, tools, *args, **kwargs)
        yield response


async def _run(tmp_path, script, *, schema=SCHEMA, **config):
    model = Recording(script)
    options = {"enable_verifier": False, "max_turns": 6, **config}
    config = AgentConfig(output_schema=schema, **options)
    result = await DefaultAgent().run(task="t", model=model,
                                      env=LocalEnvironment(workspace_root=tmp_path),
                                      tools=tools_for_names(["task_complete"]), config=config)
    return result, model


def _as_typed_child(result):
    result.metadata.update({"subagent_session_id": "child", "subagent_profile": "typed"})
    result.metadata["output_contract"].update({
        "agent_name": "typed",
        "agent_digest": "sha256:" + "1" * 64,
    })
    return result


# --- the run ---------------------------------------------------------------------------


async def test_a_valid_result_is_accepted_and_returned(tmp_path):
    result, _ = await _run(tmp_path, [_done({"count": 2, "tags": ["a"]})])
    assert result.success and result.output == {"count": 2, "tags": ["a"]}


@pytest.mark.parametrize("bad", [{"count": "two"}, {"tags": []}, {"count": 1, "extra": 1}, [1], None])
async def test_a_wrong_shape_is_repaired_then_accepted(tmp_path, bad):
    result, model = await _run(tmp_path, [_done(bad, "d1"), _done({"count": 1}, "d2")])
    assert result.success and result.output == {"count": 1}
    assert len(model.calls) == 2


async def test_it_fails_after_two_repairs_and_never_returns_the_invalid_result(tmp_path):
    bad = {"count": "x"}
    result, model = await _run(tmp_path, [_done(bad, "d1"), _done(bad, "d2"), _done(bad, "d3"),
                                          _done({"count": 1}, "d4")])
    assert not result.success and result.output is None
    assert result.metadata["error_code"] == "agent.output_invalid"
    assert result.final_message.startswith("agent.output_invalid")
    assert len(model.calls) == 3  # the first submission and two repairs, nothing more
    assert result.metadata["output_contract"]["validated"] is False
    assert "output" not in result.metadata["output_contract"]
    events = [e for e in result.metadata["events"] if e["type"] == "output_validation"]
    assert [e["payload"]["final"] for e in events] == [False, False, True]


async def test_repairs_never_outrun_the_turn_budget(tmp_path):
    bad = {"count": "x"}
    result, model = await _run(tmp_path, [_done(bad, f"d{i}") for i in range(6)], max_turns=2)
    assert not result.success and len(model.calls) <= 2


async def test_a_missing_result_is_repaired_like_a_wrong_one(tmp_path):
    result, _ = await _run(tmp_path, [_done(None, "d1"), _done({"count": 1}, "d2")])
    assert result.success and result.output == {"count": 1}


async def test_a_plain_reply_cannot_end_a_run_that_owes_a_result(tmp_path):
    prose = ModelResponse(content="all done", tool_calls=[])
    result, _ = await _run(tmp_path, [prose, _done({"count": 3})])
    assert result.success and result.output == {"count": 3}


async def test_a_valid_shape_does_not_replace_the_ordinary_gates(tmp_path):
    result, _ = await _run(
        tmp_path, [_done({"count": 1}, f"d{i}", verification_commands=["false"]) for i in range(3)],
        max_turns=3, enable_verifier=True, enable_llm_verifier=False)
    assert not result.success and result.output is None  # `false` is no verification
    assert result.metadata["output_contract"]["validated"] is False
    assert "output" not in result.metadata["output_contract"]
    verdicts = [e["payload"] for e in result.metadata["events"] if e["type"] == "verification"]
    assert verdicts and not any(v["approved"] for v in verdicts)
    assert "agent.output_invalid" not in result.final_message


async def test_without_a_schema_nothing_changes(tmp_path):
    result, _ = await _run(tmp_path, [_done()], schema=None)
    assert result.success and result.output is None


@pytest.mark.parametrize("profile", ["typed", "project/typed"])
async def test_an_ordinary_subagent_handoff_delivers_the_validated_object(tmp_path, profile):
    agents = _typed_agent(tmp_path, SCHEMA)
    accepted = {"count": 7, "tags": ["typed", "child"]}
    model = ConversationRecording([
        ModelResponse(content=None, tool_calls=[ToolCall(
            id="sub", name="invoke_subagent",
            arguments={"profile": profile, "task": "return the measured result", "handoff": "none"},
        )]),
        _done({"count": "wrong"}, "child-invalid"),
        _done(accepted, "child-done"),
        _done(call_id="parent-done"),
    ])

    result = await DefaultAgent().run(
        task="delegate and consume the typed result",
        model=model,
        env=LocalEnvironment(workspace_root=tmp_path),
        tools=default_tools(),
        config=AgentConfig(enable_verifier=False, max_turns=6, subagents=[profile]),
        agents_dir=agents,
    )

    assert result.success
    parent_continuation = model.requests[3]
    handoff = next(
        message for message in parent_continuation
        if message.role == Role.TOOL and message.name == "invoke_subagent"
    )
    child_result = handoff.metadata["child_result"]
    assert child_result["output"] == accepted
    assert child_result["schema"]["digest"].startswith("sha256:")
    assert child_result["schema"] == schema_identity(SCHEMA)
    assert child_result["producer"]["profile"] == "typed"
    assert child_result["producer"]["session_id"]
    assert child_result["producer"]["agent_digest"] == resolve_profile(
        profile, str(tmp_path), agents
    ).spec_digest
    assert set(child_result) <= {
        "version", "outcome", "producer", "schema", "delivery", "output", "workspace"
    }
    assert accepted == json.loads(handoff.content.split("Validated child result:\n", 1)[1])["output"]
    tool_event = next(
        event for event in result.metadata["events"]
        if event["type"] == "tool_result" and event["payload"].get("name") == "invoke_subagent"
    )
    assert tool_event["payload"]["child_result"] == child_result
    assert message_from_dict(message_to_dict(handoff)).metadata["child_result"] == child_result


@pytest.mark.parametrize("rewrite", ["drop_metadata", "oversized_prefix"])
async def test_post_tool_rewrite_cannot_leave_a_validated_child_claim(tmp_path, rewrite):
    from garuda.plugins.hooks import HookRegistry
    from garuda.types import ToolResult

    agents = _typed_agent(tmp_path, SCHEMA)
    model = ConversationRecording([
        ModelResponse(content=None, tool_calls=[ToolCall(
            id="sub", name="invoke_subagent",
            arguments={"profile": "typed", "task": "return it", "handoff": "none"},
        )]),
        _done({"count": 1}, "child-done"),
        _done(call_id="parent-done"),
    ])
    hooks = HookRegistry()

    async def rewrite_handoff(call, result, _context):
        if call.name != "invoke_subagent":
            return result
        return ToolResult(
            tool_call_id=result.tool_call_id,
            content=("🦅" * 1000 if rewrite == "oversized_prefix" else "[rewritten] ") + result.content,
            is_error=result.is_error,
            metadata=result.metadata if rewrite == "oversized_prefix" else {},
        )

    hooks.register_after_tool(rewrite_handoff)
    await DefaultAgent().run(
        task="delegate then finish", model=model,
        env=LocalEnvironment(workspace_root=tmp_path), tools=default_tools(),
        config=AgentConfig(
            enable_verifier=False, max_turns=5, max_output_bytes=2048, subagents=["typed"],
        ),
        agents_dir=agents, hooks=hooks,
    )

    handoff = next(
        message for message in model.requests[2]
        if message.role == Role.TOOL and message.name == "invoke_subagent"
    )
    assert handoff.content.startswith("Child result delivery unavailable:")
    assert "Validated child result" not in handoff.content
    assert "child_result" not in handoff.metadata
    assert len(handoff.content.encode("utf-8")) <= 2048


async def test_a_valid_json_null_has_explicit_accepted_presence(tmp_path):
    response = ModelResponse(content=None, tool_calls=[ToolCall(
        id="null", name="task_complete", arguments={"summary": SUMMARY, "result": None}
    )])
    result, _ = await _run(tmp_path, [response], schema={"type": "null"})

    assert result.success and result.output is None
    assert result.metadata["output_contract"]["present"] is True
    assert result.metadata["output_contract"]["validated"] is True
    assert "output" in result.metadata["output_contract"]


@pytest.mark.parametrize("accepted", [None, False, 0, "", [], {}])
async def test_explicit_json_values_survive_an_ordinary_child_handoff(tmp_path, accepted):
    schema = {"type": ["null", "boolean", "integer", "string", "array", "object"]}
    agents = _typed_agent(tmp_path, schema)
    model = ConversationRecording([
        ModelResponse(content=None, tool_calls=[ToolCall(
            id="sub", name="invoke_subagent",
            arguments={"profile": "typed", "task": "return it", "handoff": "none"},
        )]),
        _done(accepted, "child-done"),
        _done(call_id="parent-done"),
    ])

    await DefaultAgent().run(
        task="delegate then finish", model=model,
        env=LocalEnvironment(workspace_root=tmp_path), tools=default_tools(),
        config=AgentConfig(enable_verifier=False, max_turns=5, subagents=["typed"]),
        agents_dir=agents,
    )

    handoff = next(
        message for message in model.requests[2]
        if message.role == Role.TOOL and message.name == "invoke_subagent"
    )
    delivered = handoff.metadata["child_result"]["output"]
    assert delivered == accepted
    assert type(delivered) is type(accepted)


async def test_boolean_true_schema_survives_an_ordinary_child_handoff(tmp_path):
    agents = _typed_agent(tmp_path, True)
    accepted = [None, False, 0, "", [], {}]
    model = ConversationRecording([
        ModelResponse(content=None, tool_calls=[ToolCall(
            id="sub", name="invoke_subagent",
            arguments={"profile": "typed", "task": "return it", "handoff": "none"},
        )]),
        _done(accepted, "child-done"),
        _done(call_id="parent-done"),
    ])
    await DefaultAgent().run(
        task="delegate then finish", model=model,
        env=LocalEnvironment(workspace_root=tmp_path), tools=default_tools(),
        config=AgentConfig(enable_verifier=False, max_turns=5, subagents=["typed"]),
        agents_dir=agents,
    )
    handoff = next(
        message for message in model.requests[2]
        if message.role == Role.TOOL and message.name == "invoke_subagent"
    )
    assert handoff.metadata["child_result"]["output"] == accepted


async def test_parent_reassembles_a_large_unicode_child_result_through_tool_runner(tmp_path):
    schema = {
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
        "additionalProperties": False,
    }
    accepted = {"text": "🦅漢字" * 3000}
    agents = _typed_agent(tmp_path, schema)
    model = ReceiptRetrievingModel(accepted)
    buffer = ToolOutputBuffer("parent", threshold_bytes=100, root=tmp_path / "buffers")

    result = await DefaultAgent().run(
        task="delegate and retrieve the exact result", model=model,
        env=LocalEnvironment(workspace_root=tmp_path), tools=default_tools(),
        config=AgentConfig(
            enable_verifier=False, max_turns=40, max_output_bytes=2048,
            buffer_threshold_bytes=100, subagents=["typed"],
        ),
        agents_dir=agents, buffer=buffer,
    )

    assert result.success
    assert json.loads(model.rebuilt) == accepted
    slice_messages = [
        message for request in model.requests for message in request
        if message.role == Role.TOOL and message.name == "buffer_slice"
    ]
    assert slice_messages
    assert all(len(message.content.encode("utf-8")) <= 2048 for message in slice_messages)


@pytest.mark.parametrize("tamper, expected", [
    ("digest", "digest does not match"),
    ("version", "unsupported child-result reference"),
    ("format", "unsupported child-result format"),
    ("retrieval", "invalid retrieval contract"),
    ("size", "size does not match"),
    ("size_type", "invalid byte count"),
    ("parent", "another parent session"),
    ("producer", "producer does not match"),
    ("producer_prefix", "producer does not match"),
    ("buffer", "producer does not match"),
    ("malformed", "malformed child-result reference"),
    ("content", "digest does not match"),
    ("noncanonical", "not canonical JSON"),
    ("line_mode", "requires character mode"),
    ("null_reference", "malformed child-result reference"),
])
async def test_parent_path_refuses_an_invalid_child_result_receipt(tmp_path, tamper, expected):
    schema = {"type": "object", "properties": {"text": {"type": "string"}}}
    accepted = {"text": "x" * 10_000}
    agents = _typed_agent(tmp_path, schema)
    buffer = ToolOutputBuffer("parent", threshold_bytes=100, root=tmp_path / "buffers")
    model = ReceiptRetrievingModel(accepted, tamper=tamper, storage=buffer)

    await DefaultAgent().run(
        task="delegate and try the receipt", model=model,
        env=LocalEnvironment(workspace_root=tmp_path), tools=default_tools(),
        config=AgentConfig(
            enable_verifier=False, max_turns=6, max_output_bytes=2048,
            buffer_threshold_bytes=100, subagents=["typed"],
        ),
        agents_dir=agents, buffer=buffer,
    )

    response = model.requests[-1]
    retrieval = next(
        message for message in response
        if message.role == Role.TOOL and message.name == "buffer_slice"
    )
    assert expected in retrieval.content
    assert model.rebuilt == ""


async def test_a_failed_child_never_advertises_its_invalid_partial_output(tmp_path):
    agents = _typed_agent(tmp_path, SCHEMA)
    invalid = {"count": "still wrong"}
    model = ConversationRecording([
        ModelResponse(content=None, tool_calls=[ToolCall(
            id="sub", name="invoke_subagent",
            arguments={"profile": "typed", "task": "return a result", "handoff": "none"},
        )]),
        _done(invalid, "bad-1"),
        _done(invalid, "bad-2"),
        _done(invalid, "bad-3"),
        _done(call_id="parent-done"),
    ])

    result = await DefaultAgent().run(
        task="delegate then finish", model=model,
        env=LocalEnvironment(workspace_root=tmp_path), tools=default_tools(),
        config=AgentConfig(enable_verifier=False, max_turns=7, subagents=["typed"]),
        agents_dir=agents,
    )

    handoff = next(
        message for message in model.requests[4]
        if message.role == Role.TOOL and message.name == "invoke_subagent"
    )
    assert handoff.metadata["child_result"]["outcome"]["validation"] == "not_accepted"
    assert handoff.metadata["child_result"]["delivery"]["state"] == "unavailable"
    assert "output" not in handoff.metadata["child_result"]
    status = json.loads(handoff.content.split("Child result status:\n", 1)[1])
    assert status.get("output") != invalid and "output" not in status
    assert result.success


async def test_a_large_child_result_publishes_a_complete_parent_reference(tmp_path):
    schema = {
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
        "additionalProperties": False,
    }
    agents = _typed_agent(tmp_path, schema)
    accepted = {"text": "🦅" + "single-line-value" * 900}
    model = ConversationRecording([
        ModelResponse(content=None, tool_calls=[ToolCall(
            id="sub", name="invoke_subagent",
            arguments={"profile": "typed", "task": "return the large result", "handoff": "none"},
        )]),
        _done(accepted, "child-done"),
        _done(call_id="parent-done"),
    ])
    buffer = ToolOutputBuffer("parent", threshold_bytes=100, root=tmp_path / "buffers")

    await DefaultAgent().run(
        task="delegate then finish", model=model,
        env=LocalEnvironment(workspace_root=tmp_path), tools=default_tools(),
        config=AgentConfig(enable_verifier=False, max_turns=5, subagents=["typed"]),
        agents_dir=agents, buffer=buffer,
    )

    handoff = next(
        message for message in model.requests[2]
        if message.role == Role.TOOL and message.name == "invoke_subagent"
    )
    delivery = handoff.metadata["child_result"]["delivery"]
    assert delivery["state"] == "referenced" and "output" not in handoff.metadata["child_result"]
    reference = delivery["reference"]
    assert buffer.read(reference["buffer_id"]) == json.dumps(
        accepted, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


async def test_child_changed_files_come_from_its_workspace_delta(tmp_path):
    agents = _typed_agent(
        tmp_path,
        SCHEMA,
        extends="garuda/build",
        extra="completion: {acceptance_contract: false}\n",
    )
    (tmp_path / "preexisting.txt").write_text("clean\n")
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "Test"], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.email", "test@example.invalid"], check=True
    )
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-qm", "baseline"], check=True)
    (tmp_path / "preexisting.txt").write_text("dirty before child\n")
    accepted = {"count": 1}
    model = ConversationRecording([
        ModelResponse(content=None, tool_calls=[ToolCall(
            id="sub", name="invoke_subagent",
            arguments={"profile": "typed", "task": "write the result", "handoff": "none"},
        )]),
        ModelResponse(content=None, tool_calls=[
            ToolCall(
                id="write", name="write_file",
                arguments={"path": "produced.txt", "content": "ok\n"},
            ),
            ToolCall(
                id="rewrite", name="write_file",
                arguments={"path": "preexisting.txt", "content": "changed again\n"},
            ),
        ]),
        _done(accepted, "child-done", verification_commands=["test -f produced.txt"]),
        _done(call_id="parent-done"),
    ])

    await DefaultAgent().run(
        task="delegate then finish", model=model,
        env=LocalEnvironment(workspace_root=tmp_path), tools=default_tools(),
        config=AgentConfig(
            enable_verifier=False, max_turns=6, subagents=["typed"], permission_mode="yolo"
        ),
        agents_dir=agents,
    )

    handoff = next(
        message for message in model.requests[3]
        if message.role == Role.TOOL and message.name == "invoke_subagent"
    )
    child_write = next(
        message for message in model.requests[2]
        if message.role == Role.TOOL and message.name == "write_file"
    )
    assert (tmp_path / "produced.txt").exists(), child_write.content
    workspace = handoff.metadata["child_result"]["workspace"]
    assert workspace["attribution"] == "captured"
    assert workspace["changed_count"] == 2
    assert workspace["files"] == [
        {"path": "preexisting.txt", "kind": "modified", "preexisting_at_start": True},
        {"path": "produced.txt", "kind": "untracked"},
    ]
    assert "Files changed: preexisting.txt, produced.txt" in handoff.content


async def test_docker_child_without_a_proved_host_root_refuses_local_attribution(
    tmp_path, monkeypatch
):
    agents = _typed_agent(tmp_path, SCHEMA)
    model = ConversationRecording([
        ModelResponse(content=None, tool_calls=[ToolCall(
            id="sub", name="invoke_subagent",
            arguments={"profile": "typed", "task": "return it", "handoff": "none"},
        )]),
        _done({"count": 1}, "child-done"),
        _done(call_id="parent-done"),
    ])

    def unexpected_capture(_workspace):
        raise AssertionError("an unrelated host path must not be inspected")

    monkeypatch.setattr("garuda.workspace.evidence.capture_baseline", unexpected_capture)
    await DefaultAgent().run(
        task="delegate then finish", model=model,
        env=LocalEnvironment(workspace_root="/workspace"), tools=default_tools(),
        config=AgentConfig(
            enable_verifier=False, max_turns=5, subagents=["typed"], workspace_kind="docker"
        ),
        agents_dir=agents,
    )

    handoff = next(
        message for message in model.requests[2]
        if message.role == Role.TOOL and message.name == "invoke_subagent"
    )
    assert handoff.metadata["child_result"]["workspace"] == {
        "kind": "docker",
        "attribution": "unavailable_host_root",
    }


async def test_a_large_result_is_explicitly_unavailable_without_parent_retrieval(tmp_path):
    schema = {
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
    }
    agents = _typed_agent(tmp_path, schema)
    model = ConversationRecording([
        ModelResponse(content=None, tool_calls=[ToolCall(
            id="sub", name="invoke_subagent",
            arguments={"profile": "typed", "task": "return it", "handoff": "none"},
        )]),
        _done({"text": "x" * 10_000}, "child-done"),
        _done(call_id="parent-done"),
    ])
    buffer = ToolOutputBuffer("parent", threshold_bytes=100, root=tmp_path / "buffers")

    await DefaultAgent().run(
        task="delegate then finish", model=model,
        env=LocalEnvironment(workspace_root=tmp_path),
        tools=tools_for_names(["invoke_subagent", "task_complete"]),
        config=AgentConfig(enable_verifier=False, max_turns=5, subagents=["typed"]),
        agents_dir=agents, buffer=buffer,
    )

    handoff = next(
        message for message in model.requests[2]
        if message.role == Role.TOOL and message.name == "invoke_subagent"
    )
    envelope = handoff.metadata["child_result"]
    assert envelope["delivery"] == {
        "state": "unavailable", "reason": "parent_retrieval_tool_unavailable"
    }
    assert "output" not in envelope and "reference" not in envelope["delivery"]


async def test_publication_failure_and_disabled_storage_are_explicit(tmp_path, monkeypatch):
    schema = {
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
    }
    validated, _ = await _run(tmp_path, [_done({"text": "x" * 10_000})], schema=schema)
    _as_typed_child(validated)
    buffer = ToolOutputBuffer("parent", threshold_bytes=100, root=tmp_path / "buffers")

    disabled = build_child_result_envelope(
        validated, "typed", storage=None, parent_tools=default_tools(), output_budget=30_720
    )
    assert disabled["delivery"] == {
        "state": "unavailable", "reason": "parent_storage_unavailable"
    }

    def fail_publication(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(buffer, "store", fail_publication)
    failed = build_child_result_envelope(
        validated, "typed", storage=buffer, parent_tools=default_tools(), output_budget=30_720
    )
    assert failed["delivery"] == {"state": "unavailable", "reason": "publication_failed"}
    assert "output" not in failed


async def test_child_result_recording_rejects_undeclared_nested_metadata(tmp_path):
    validated, _ = await _run(tmp_path, [_done({"count": 1})])
    _as_typed_child(validated)
    envelope = build_child_result_envelope(
        validated, "typed", storage=None, parent_tools=default_tools(), output_budget=4096
    )
    assert validate_child_result_envelope(envelope) == envelope
    for path in (("extra",), ("producer", "extra"), ("delivery", "extra"), ("schema", "extra")):
        changed = deepcopy(envelope)
        target = changed
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = "not part of the contract"
        assert validate_child_result_envelope(changed) is None
    wrong_types = [
        (("version",), True),
        (("outcome", "success"), 1),
        (("delivery", "serialized_bytes"), True),
    ]
    for path, value in wrong_types:
        changed = deepcopy(envelope)
        target = changed
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value
        assert validate_child_result_envelope(changed) is None


@pytest.mark.parametrize("tamper", ["schema", "producer"])
async def test_handoff_recomputes_schema_and_producer_identity_before_delivery(tmp_path, tamper):
    validated, _ = await _run(tmp_path, [_done({"count": 1})])
    _as_typed_child(validated)
    if tamper == "schema":
        validated.metadata["output_contract"]["schema_document"] = {"type": "string"}
    else:
        validated.metadata["output_contract"]["agent_name"] = "another-profile"

    envelope = build_child_result_envelope(
        validated, "typed", storage=None, parent_tools=default_tools(), output_budget=4096
    )
    assert envelope["outcome"]["validation"] == "not_accepted"
    assert envelope["delivery"]["state"] == "unavailable"
    assert "output" not in envelope


async def test_handoff_envelope_accounts_for_wrapper_and_long_workspace_identity(tmp_path):
    validated, _ = await _run(tmp_path, [_done({"count": 1})])
    _as_typed_child(validated)
    workspace = {
        "kind": "local",
        "attribution": "captured",
        "root": "/" + "界" * 1000,
        "changed_count": 1,
        "files": [{"path": "界" * 1000, "kind": "modified"}],
    }
    envelope = build_child_result_envelope(
        validated,
        "typed",
        storage=ToolOutputBuffer("parent", threshold_bytes=100, root=tmp_path / "buffers"),
        parent_tools=default_tools(),
        output_budget=1024,
        workspace=workspace,
    )
    rendered = render_child_result_handoff(SUMMARY * 100, envelope, 1024)
    assert len(rendered.encode("utf-8")) <= 1024
    assert envelope["workspace"]["root_omitted"] is True
    assert envelope["workspace"]["files_omitted"] == 1
    assert "界" * 20 not in json.dumps(envelope, ensure_ascii=False)

    suffix = "Validated child result:\n" + canonical_json(envelope)
    tight_budget = len(suffix.encode("utf-8")) + 5
    tight = render_child_result_handoff(SUMMARY, envelope, tight_budget)
    assert tight == suffix
    assert len(tight.encode("utf-8")) <= tight_budget


# --- the definition --------------------------------------------------------------------


@pytest.fixture
def agents(tmp_path):
    directory = tmp_path / "ws" / ".agent" / "agents"
    (directory / "schemas").mkdir(parents=True)
    return directory


def _schema(agents, doc, name="result.json"):
    (agents / "schemas" / name).write_text(doc if isinstance(doc, str) else json.dumps(doc))


def _load(agents, rel="schemas/result.json", name="a", extends="garuda/explore"):
    (agents / f"{name}.yaml").write_text(
        f"version: 1\nextends: {extends}\noutput: {{schema: {rel}}}\n")
    return load_profile(name, extra_dir=agents)


def test_a_declared_schema_is_compiled_into_the_profile_and_config(agents):
    _schema(agents, SCHEMA)
    profile = _load(agents)
    assert profile.output_schema == SCHEMA
    assert profile.to_agent_config().output_schema == SCHEMA


@pytest.mark.parametrize("doc, code", [
    ({"$ref": "https://example.com/schema.json"}, "agent.output_schema_ref"),
    ({"properties": {"a": {"$ref": "other.json#/x"}}}, "agent.output_schema_ref"),
    ({"properties": {"a": {"$ref": "../../../etc/schema.json"}}}, "agent.output_schema_ref"),
    ({"$ref": "#/$defs/missing"}, "agent.output_schema_ref"),
    ({"$defs": {"a": {"$ref": "#/$defs/a"}}, "$ref": "#/$defs/a"}, "agent.output_schema_ref"),
    ({"format": "date-time"}, "agent.output_schema_unsupported"),
    ({"pattern": "(a+)+$"}, "agent.output_schema_unsupported"),
    ({"$id": "https://example.com/x"}, "agent.output_schema_unsupported"),
    ({"$dynamicRef": "#x"}, "agent.output_schema_unsupported"),
    ({"madeUpKeyword": 1}, "agent.output_schema_unsupported"),
    ({"$schema": "http://json-schema.org/draft-07/schema#"}, "agent.output_schema_unsupported"),
    ({"type": "nonsense"}, "agent.output_schema_invalid"),
    ("{not json", "agent.output_schema_invalid"),
    ("[1, 2]", "agent.output_schema_invalid"),
])
def test_a_schema_that_cannot_be_trusted_refuses_when_resolved(agents, doc, code):
    _schema(agents, doc)
    with pytest.raises(ConfigError, match=code):
        _load(agents)


def test_reference_expansion_exhaustion_refuses(agents):
    defs = {"d0": {"type": "integer"}}
    for i in range(1, 12):
        defs[f"d{i}"] = {"allOf": [{"$ref": f"#/$defs/d{i - 1}"}] * 3}
    _schema(agents, {"$defs": defs, "$ref": "#/$defs/d11"})
    with pytest.raises(ConfigError, match="agent.output_schema_too_large"):
        _load(agents)


def test_size_depth_and_reference_count_are_bounded(agents):
    _schema(agents, {"description": "x" * 70_000})
    with pytest.raises(ConfigError, match="agent.output_schema_too_large"):
        _load(agents)
    deep: dict = {"type": "integer"}
    for _ in range(40):
        deep = {"items": deep}
    _schema(agents, deep)
    with pytest.raises(ConfigError, match="agent.output_schema_too_large"):
        _load(agents)
    _schema(agents, {"$defs": {"x": {"type": "integer"}},
                     "allOf": [{"$ref": "#/$defs/x"}] * 70})
    with pytest.raises(ConfigError, match="agent.output_schema_too_large"):
        _load(agents)


def test_the_schema_path_stays_inside_the_agents_root(agents, tmp_path):
    (tmp_path / "outside.json").write_text("{}")
    with pytest.raises(ConfigError, match="agent.path_escapes"):
        _load(agents, "../../../outside.json")
    with pytest.raises(ConfigError, match="agent.path_escapes"):
        _load(agents, str(tmp_path / "outside.json"))
    (agents / "schemas" / "link.json").symlink_to(tmp_path / "outside.json")
    with pytest.raises(ConfigError, match="agent.path_escapes"):
        _load(agents, "schemas/link.json")
    with pytest.raises(ConfigError, match="agent.output_schema_invalid"):
        _load(agents, "schemas/nope.json")


def test_a_child_inherits_the_schema_and_null_drops_it(agents):
    _schema(agents, SCHEMA)
    _load(agents, name="parent")
    (agents / "child.yaml").write_text("version: 1\nextends: project/parent\n")
    assert load_profile("child", extra_dir=agents).output_schema == SCHEMA
    (agents / "plain.yaml").write_text("version: 1\nextends: project/parent\noutput: {schema: null}\n")
    assert load_profile("plain", extra_dir=agents).output_schema is None


async def test_the_terminal_tool_asks_for_the_result_only_when_a_schema_is_declared(tmp_path):
    def done_tool(model):
        (entry,) = [t for t in model.calls[0] if t["function"]["name"] == "task_complete"]
        return entry["function"]["parameters"]

    _, with_schema = await _run(tmp_path, [_done({"count": 1})])
    parameters = done_tool(with_schema)
    assert "result" in parameters["required"]
    assert "Draft 2020-12" in parameters["properties"]["result"]["description"]

    _, plain = await _run(tmp_path, [_done()], schema=None)
    assert "result" not in done_tool(plain)["properties"]


def test_check_reports_a_refusal_with_its_code(agents, capsys, monkeypatch):
    from tests.test_runtime_cli import _main

    _schema(agents, {"$ref": "http://example.com/x"})
    (agents / "a.yaml").write_text("version: 1\nextends: garuda/explore\n"
                                   "output: {schema: schemas/result.json}\n")
    code, out = _main(monkeypatch, capsys, "agent", "check", "a", "--json",
                      "--workspace", str(agents.parents[1]))
    (diag,) = json.loads(out)
    assert code == 1 and diag["code"] == "agent.output_schema_ref" and diag["fix"]


async def test_a_delegated_terminal_tool_does_not_carry_the_parents_schema(tmp_path):
    from garuda.tools.task_complete import TaskCompleteTool

    model = Recording([_done()])
    await DefaultAgent().run(
        task="t", model=model, env=LocalEnvironment(workspace_root=tmp_path),
        tools=[TaskCompleteTool.with_output(SCHEMA)],
        config=AgentConfig(enable_verifier=False, max_turns=2))
    (entry,) = model.calls[0]
    assert "result" not in entry["function"]["parameters"]["properties"]
