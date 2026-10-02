"""Prompt assembly from labelled sections, and memory sources (#162, plan task H.4)."""

import os

import pytest

from garuda.agents.loader import load_profile, resolve_system_prompt, system_prompt_sections
from garuda.agents.resolve import user_agents_dir
from garuda.model.config import ConfigError
from garuda.types import DEFAULT_SYSTEM_PROMPT


@pytest.fixture
def ws(tmp_path):
    root = tmp_path / "ws"
    (root / ".agent" / "agents").mkdir(parents=True)
    return root


def _agent(ws, text, name="a"):
    (ws / ".agent" / "agents" / f"{name}.yaml").write_text("version: 1\n" + text)
    return load_profile(name, extra_dir=ws / ".agent" / "agents")


def _old(profile, ws):
    """The post-H.0 algorithm: instructions, skills, the first project memory file."""
    text = profile.system_prompt or DEFAULT_SYSTEM_PROMPT
    for name in ("AGENTS.md", "GARUDA.md"):
        path = ws / name
        if path.is_file():
            body = path.read_text()[:8000]
            return text + f"\n\n## Project instructions (from {name})\n{body}"
    return text


def _user_agents_md(text):
    home = user_agents_dir().parent
    home.mkdir(parents=True, exist_ok=True)
    (home / "AGENTS.md").write_text(text)


@pytest.mark.parametrize("memory", [None, "Use tabs.\n", "GARUDA"])
def test_zero_config_is_byte_identical(ws, memory):
    _user_agents_md("user notes that a legacy profile never loaded")
    if memory == "GARUDA":
        (ws / "GARUDA.md").write_text("garuda rules\n")
    elif memory:
        (ws / "AGENTS.md").write_text(memory)
    for name in ("explore", "build", "plan"):
        profile = load_profile(name)
        assert resolve_system_prompt(profile, ws) == _old(profile, ws)


def test_sections_come_in_the_fixed_order(ws):
    _user_agents_md("Always answer in English.")
    (ws / "AGENTS.md").write_text("Project rule.")
    (ws / ".context").mkdir()
    (ws / ".context" / "decisions.md").write_text("We chose X.")
    profile = _agent(ws, "extends: garuda/explore\nmemory: {context_pack: true}\n")

    kinds = [kind for kind, _source, _text in system_prompt_sections(profile, ws)]
    assert kinds == ["instructions", "user_memory", "project_memory", "context_pack"]
    prompt = resolve_system_prompt(profile, ws)
    assert prompt.index("Always answer in English.") < prompt.index("Project rule.") < \
        prompt.index("We chose X.")
    assert "## Project instructions (from AGENTS.md)" in prompt  # labelled as project text


def test_project_mode_all_and_a_custom_list(ws):
    (ws / "AGENTS.md").write_text("MARK-ONE")
    (ws / "GARUDA.md").write_text("MARK-TWO")
    (ws / "docs").mkdir()
    (ws / "docs" / "rules.md").write_text("MARK-THREE")
    first = resolve_system_prompt(_agent(ws, "memory: {user: false}\n", "f"), ws)
    every = resolve_system_prompt(_agent(ws, "memory: {user: false, project_mode: all}\n", "e"), ws)
    custom = resolve_system_prompt(_agent(ws, "memory: {project: [docs/rules.md]}\n", "c"), ws)
    assert "MARK-ONE" in first and "MARK-TWO" not in first
    assert "MARK-ONE" in every and "MARK-TWO" in every
    assert "MARK-THREE" in custom and "MARK-ONE" not in custom


def test_over_budget_trims_the_context_pack_then_project_memory(ws):
    (ws / "AGENTS.md").write_text("project paragraph\n\n" * 400)
    (ws / ".context").mkdir()
    (ws / ".context" / "architecture.md").write_text("context paragraph\n\n" * 400)
    profile = _agent(ws, "extends: garuda/explore\nmemory: {user: false, context_pack: true,"
                         " max_total_chars: 9000}\n")
    diagnostics = []
    sections = system_prompt_sections(profile, ws, diagnostics=diagnostics)
    text = "".join(t for _k, _s, t in sections)

    assert len(text) <= 9000
    assert text.startswith(profile.system_prompt or DEFAULT_SYSTEM_PROMPT)  # never cut
    trimmed = [d["section"] for d in diagnostics if d["code"] == "memory.trimmed"]
    assert trimmed[0] == ".context/architecture.md"
    assert "[trimmed to fit the prompt budget]" in text


def test_oversized_instructions_refuse(ws):
    with pytest.raises(ConfigError, match="agent.instructions_too_large"):
        resolve_system_prompt(_agent(ws, "instructions: {mode: replace, text: '"
                                         + "x" * 2000 + "'}\nmemory: {max_total_chars: 1000}\n"),
                              ws)


def test_a_mandatory_section_over_the_token_budget_refuses(ws):
    profile = _agent(ws, "instructions: {mode: replace, text: '" + "y" * 12000 + "'}\n"
                         "context: {max_tokens: 4000, reserved_output_tokens: 1000,"
                         " safety_margin_tokens: 500, summarize_after_tokens: 100}\n")
    with pytest.raises(ConfigError, match="agent.prompt_over_budget"):
        resolve_system_prompt(profile, ws)


def test_memory_cannot_escape_the_workspace(ws, tmp_path):
    outside = tmp_path / "outside.md"
    outside.write_text("secret")
    with pytest.raises(ConfigError, match="memory.path_escapes"):
        resolve_system_prompt(_agent(ws, "memory: {project: [../outside.md]}\n", "dots"), ws)
    (ws / "AGENTS.md").symlink_to(outside)
    with pytest.raises(ConfigError, match="memory.path_escapes"):
        resolve_system_prompt(_agent(ws, "memory: {user: false}\n", "link"), ws)
    diagnostics = []
    legacy = resolve_system_prompt(load_profile("explore"), ws, diagnostics=diagnostics)
    assert "secret" not in legacy  # a legacy profile skips it, with a diagnostic
    assert diagnostics[0]["code"] == "memory.path_escapes"


def test_an_in_repository_symlink_still_works(ws):
    (ws / "CLAUDE.md").write_text("shared rules")
    os.symlink("CLAUDE.md", ws / "AGENTS.md")
    assert "shared rules" in resolve_system_prompt(load_profile("explore"), ws)


async def test_a_source_changed_after_inspection_shows_in_the_actual_digest(ws):
    from garuda.agents import inspect
    from tests.test_agent_cli import _first_system

    (ws / "AGENTS.md").write_text("before")
    static = inspect.prompt("explore", ws)
    (ws / "AGENTS.md").write_text("after a teammate edited it")
    _sent, actual = await _first_system(ws, bootstrap=False)
    assert actual[0]["payload"]["digest"] != static["digest"]
