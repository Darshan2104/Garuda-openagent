"""Feature 3: multimodal tool-result content blocks."""

from pathlib import Path

from garuda.core.loop import DefaultAgent
from garuda.model.litellm_model import LitellmModel, _message_to_litellm
from garuda.model.protocol import ModelResponse
from garuda.tools import default_tools
from garuda.tools.protocol import ToolContext
from garuda.types import AgentConfig, Message, Role, ToolCall, ToolResult
from garuda.workspace.local import LocalEnvironment

_URI = "data:image/png;base64,AAAA"


# --- serialization: images -> content blocks, gated on vision support -------

def test_message_with_images_serializes_blocks_when_included():
    msg = Message(role=Role.USER, content="see this", images=[_URI])
    payload = _message_to_litellm(msg, include_images=True)
    assert isinstance(payload["content"], list)
    assert payload["content"][0] == {"type": "text", "text": "see this"}
    assert payload["content"][1] == {"type": "image_url", "image_url": {"url": _URI}}


def test_message_with_images_dropped_when_not_included():
    msg = Message(role=Role.USER, content="see this", images=[_URI])
    payload = _message_to_litellm(msg, include_images=False)
    assert payload["content"] == "see this"  # plain text; image dropped


def test_build_kwargs_gates_images_on_vision(monkeypatch):
    m = LitellmModel(model_name="some/vision-model")
    msgs = [Message(role=Role.USER, content="hi", images=[_URI])]

    monkeypatch.setattr(m, "_supports_vision", lambda: True)
    kwargs = m._build_kwargs(msgs)
    assert isinstance(kwargs["messages"][0]["content"], list)  # blocks kept

    monkeypatch.setattr(m, "_supports_vision", lambda: False)
    kwargs = m._build_kwargs(msgs)
    assert kwargs["messages"][0]["content"] == "hi"  # dropped for non-vision model


# --- ToolResult carries images through the tool boundary --------------------

def test_toolresult_has_images_field():
    tr = ToolResult(tool_call_id="", content="x", images=[_URI])
    assert tr.images == [_URI]


# --- loop appends a user image message from tool-returned images ------------

class _ImageTool:
    name = "snap"
    description = "returns an image"
    parameters = {"type": "object", "properties": {}, "required": []}

    async def execute(self, arguments, env, ctx):
        return ToolResult(tool_call_id="", content="snapped", images=[_URI])


async def test_loop_emits_user_image_message(tmp_path: Path):
    from garuda.model.script_model import ScriptModel

    env = LocalEnvironment(workspace_root=tmp_path)
    tools = default_tools() + [_ImageTool()]
    model = ScriptModel(responses=[
        ModelResponse(content=None, tool_calls=[ToolCall(id="s", name="snap", arguments={})]),
        ModelResponse(content=None, tool_calls=[ToolCall(id="d", name="task_complete",
                      arguments={"summary": "Looked at the snapshot and finished."})]),
    ])
    result = await DefaultAgent().run(
        task="t", model=model, env=env, tools=tools, config=AgentConfig(max_turns=5),
    )
    img_msgs = [m for m in result.messages if m.role == Role.USER and m.images]
    assert img_msgs, "no user image message emitted from tool images"
    assert img_msgs[0].images == [_URI]
    # It comes after the tool result (contiguity preserved).
    tool_idx = next(i for i, m in enumerate(result.messages) if m.role == Role.TOOL and m.name == "snap")
    img_idx = next(i for i, m in enumerate(result.messages) if m.role == Role.USER and m.images)
    assert img_idx > tool_idx
