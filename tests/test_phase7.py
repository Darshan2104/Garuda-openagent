"""Tests for agent capabilities: agent.md, skills, SDK, documents, permissions."""

import tempfile
from pathlib import Path

import pytest

from garuda.agents.frontmatter import parse_frontmatter
from garuda.agents.loader import list_profiles, load_profile, resolve_system_prompt
from garuda.agents.md_loader import load_agent_md
from garuda.core.permissions import PermissionEngine
from garuda.mcp.config import load_mcp_config
from garuda.skills.loader import discover_skills, format_skills_prompt, load_skill
from garuda.tools.registry import list_tool_names, register_tool
from garuda.tools.task_complete import TaskCompleteTool


def test_parse_frontmatter():
    text = "---\nname: demo\ndescription: test\n---\n\nBody here"
    meta, body = parse_frontmatter(text)
    assert meta["name"] == "demo"
    assert body == "Body here"


def test_load_reviewer_agent_md():
    profile = load_profile("reviewer")
    assert profile.permission_mode == "readonly"
    assert "read_file" in (profile.tools or [])


def test_list_profiles_includes_reviewer():
    assert "reviewer" in list_profiles()


def test_skill_loader(tmp_path):
    skill_dir = tmp_path / "pdf-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nname: pdf-processing\ndescription: Work with PDFs\n---\n\nExtract text carefully.\n",
        encoding="utf-8",
    )
    skills = discover_skills(skill_dir)
    assert len(skills) == 1
    assert skills[0].name == "pdf-processing"
    prompt = format_skills_prompt(skills)
    assert "pdf-processing" in prompt


def test_resolve_system_prompt_with_skills(tmp_path):
    skill_dir = tmp_path / "skills" / "demo-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: demo-skill\ndescription: Demo\n---\n\nSkill body.",
        encoding="utf-8",
    )
    profile = load_profile("build")
    profile.skills = ["demo-skill"]
    profile.skills_dirs = [str(tmp_path / "skills")]
    prompt = resolve_system_prompt(profile, tmp_path)
    assert "demo-skill" in prompt


def test_path_permission_rules():
    engine = PermissionEngine(
        mode="smart",
        path_rules={"deny": ["**/.env", "**/secrets/*"]},
    )
    allowed, _ = __import__("asyncio").run(engine.evaluate_tool_call("read_file", {"path": ".env"}))
    assert not allowed


def test_mcp_env_interpolation(tmp_path, monkeypatch):
    monkeypatch.setenv("MY_TOKEN", "secret123")
    config = tmp_path / "mcp.yaml"
    config.write_text(
        "servers:\n  - name: echo\n    transport: stdio\n    command: echo\n    args: []\n    env:\n      TOKEN: ${MY_TOKEN}\n",
        encoding="utf-8",
    )
    servers = load_mcp_config(config)
    assert servers[0].env["TOKEN"] == "secret123"


def test_tool_registry_register():
    tool = TaskCompleteTool()
    register_tool(tool, replace=True)
    assert "task_complete" in list_tool_names()


def test_document_tools_registered():
    names = list_tool_names()
    assert "read_pdf" in names
    assert "read_spreadsheet" in names


@pytest.mark.asyncio
async def test_software_agent_sdk(tmp_path):
    from garuda.core.loop import DefaultAgent
    from garuda.model.protocol import ModelResponse
    from garuda.model.script_model import ScriptModel
    from garuda.sdk import SoftwareAgent
    from garuda.types import ToolCall

    agent = SoftwareAgent(workspace=str(tmp_path), model="script/test", agent="build", mode="standard")

    import garuda.interfaces.runner as runner_mod

    orig = runner_mod.resolve_environment

    async def local_env(*args, **kwargs):
        from garuda.workspace.local import LocalEnvironment

        env = LocalEnvironment(workspace_root=tmp_path)
        return env, None

    runner_mod.resolve_environment = local_env

    import garuda.sdk.software_agent as sdk_mod

    orig_model = sdk_mod.LitellmModel
    sdk_mod.LitellmModel = lambda model_name, **kw: ScriptModel(
        [
            ModelResponse(
                content=None,
                tool_calls=[
                    ToolCall(id="1", name="task_complete", arguments={"summary": "SDK smoke test completed fine."})
                ],
            )
        ]
    )
    try:
        result = await agent.run("sdk smoke test")
    finally:
        sdk_mod.LitellmModel = orig_model
        runner_mod.resolve_environment = orig

    assert result.success
