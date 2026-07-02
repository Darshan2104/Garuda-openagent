import pytest

from garuda.agents.loader import list_profiles, load_profile
from garuda.context.shaper import shape_observation
from garuda.core.loop import DefaultAgent
from garuda.core.permissions import PermissionDecision, PermissionEngine
from garuda.core.verifier import CompletionVerifier
from garuda.model.protocol import ModelResponse
from garuda.model.script_model import ScriptModel
from garuda.tools import default_tools, tools_for_names
from garuda.tools.edit import EditTool
from garuda.tools.protocol import ToolContext
from garuda.types import AgentConfig, ToolCall
from garuda.workspace.local import LocalEnvironment


def test_shape_observation_truncates():
    text = "x" * 1000
    shaped = shape_observation(text, max_bytes=100)
    assert "truncated" in shaped
    assert len(shaped.encode("utf-8")) < len(text.encode("utf-8"))


def test_permission_engine_readonly_denies_write():
    engine = PermissionEngine(mode="readonly")
    assert engine.check_tool("write_file") == PermissionDecision.DENY
    assert engine.check_tool("read_file") == PermissionDecision.ALLOW


@pytest.mark.asyncio
async def test_permission_engine_denies_rm_rf_root():
    engine = PermissionEngine(mode="smart")
    assert engine.check_command("rm -rf /") == PermissionDecision.DENY


@pytest.mark.asyncio
async def test_edit_tool_replaces_string(tmp_path):
    env = LocalEnvironment(workspace_root=tmp_path)
    await env.write_file("greeting.txt", "hello\nworld\n")
    tool = EditTool()
    result = await tool.execute(
        {"path": "greeting.txt", "old_string": "world", "new_string": "garuda"},
        env,
        ToolContext(session_id="test"),
    )
    assert not result.is_error
    updated = await env.read_file("greeting.txt")
    assert "garuda" in updated
    assert "world" not in updated


def test_agent_profiles_load():
    names = list_profiles()
    assert "build" in names
    profile = load_profile("plan")
    assert profile.permission_mode == "readonly"
    assert "write_file" not in (profile.tools or [])


@pytest.mark.asyncio
async def test_task_complete_verification(tmp_path):
    env = LocalEnvironment(workspace_root=tmp_path)
    await env.write_file("done.txt", "yes")
    model = ScriptModel(
        responses=[
            ModelResponse(
                content=None,
                tool_calls=[
                    ToolCall(
                        id="1",
                        name="task_complete",
                        arguments={
                            "summary": "Created done.txt with verification.",
                            "verification_commands": ["test -f done.txt"],
                        },
                    ),
                ],
            ),
        ]
    )
    agent = DefaultAgent(profile_name="build")
    result = await agent.run(
        task="Create done.txt",
        model=model,
        env=env,
        tools=tools_for_names(["task_complete", "bash"]),
        config=AgentConfig(max_turns=3, enable_verifier=True),
    )
    assert result.success


@pytest.mark.asyncio
async def test_verifier_rejects_short_summary(tmp_path):
    env = LocalEnvironment(workspace_root=tmp_path)
    verifier = CompletionVerifier()
    result = await verifier.verify_with_commands(
        task="do thing",
        summary="done",
        verification_commands=[],
        env=env,
        config=AgentConfig(enable_verifier=True),
    )
    assert not result.approved
