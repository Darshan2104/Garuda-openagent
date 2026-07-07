"""B3: skills allowed-tools — surfaced in the index + validated at load."""

import logging
from pathlib import Path

from garuda.agents.loader import AgentProfile, resolve_system_prompt
from garuda.skills.loader import Skill, format_skills_prompt, load_skill


def _write_skill(root: Path, name: str, allowed: str | None = None) -> Path:
    d = root / name
    d.mkdir(parents=True)
    fm = f"name: {name}\ndescription: does {name}\n"
    if allowed is not None:
        fm += f"allowed-tools: {allowed}\n"
    (d / "SKILL.md").write_text(f"---\n{fm}---\nBody of {name}.\n", encoding="utf-8")
    return d / "SKILL.md"


def test_allowed_tools_parsed_from_frontmatter(tmp_path: Path):
    path = _write_skill(tmp_path, "deploy", allowed="bash, read_file")
    skill = load_skill(path)
    assert skill.allowed_tools == ["bash", "read_file"]


def test_index_line_shows_tools_when_present_else_omits():
    with_tools = Skill(name="a", description="d", body="", allowed_tools=["bash", "edit"])
    without = Skill(name="b", description="d", body="")
    assert "[tools: bash, edit]" in with_tools.to_index_line()
    assert "[tools:" not in without.to_index_line()


def test_prompt_includes_restriction_instruction():
    skills = [Skill(name="a", description="d", body="", allowed_tools=["bash"])]
    prompt = format_skills_prompt(skills)
    assert "restrict yourself to those tools" in prompt
    assert "[tools: bash]" in prompt


def test_resolve_warns_on_ungranted_tool(tmp_path: Path, caplog):
    _write_skill(tmp_path / ".agent" / "skills", "deploy", allowed="kubectl_apply")
    profile = AgentProfile(name="t", system_prompt="BASE", tools=["bash", "read_file"])
    with caplog.at_level(logging.WARNING):
        resolve_system_prompt(profile, tmp_path)
    assert "kubectl_apply" in caplog.text


def test_resolve_no_warning_when_tools_unrestricted(tmp_path: Path, caplog):
    _write_skill(tmp_path / ".agent" / "skills", "deploy", allowed="anything_goes")
    profile = AgentProfile(name="t", system_prompt="BASE", tools=None)  # all builtins
    with caplog.at_level(logging.WARNING):
        resolve_system_prompt(profile, tmp_path)
    assert "anything_goes" not in caplog.text


def test_resolve_no_warning_when_tool_granted_or_mcp(tmp_path: Path, caplog):
    _write_skill(tmp_path / ".agent" / "skills", "deploy", allowed="bash, mcp__srv__do")
    profile = AgentProfile(name="t", system_prompt="BASE", tools=["bash"])
    with caplog.at_level(logging.WARNING):
        resolve_system_prompt(profile, tmp_path)
    # bash is granted; mcp__* is skipped -> no warnings about either
    assert "bash" not in caplog.text and "mcp__srv__do" not in caplog.text
