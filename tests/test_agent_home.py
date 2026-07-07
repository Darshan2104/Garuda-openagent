"""§1: the `.agent/` project-home resolver and its integration points."""

from pathlib import Path

from garuda.config.agent_home import resolve_agent_home, resolve_agents_dir


def test_empty_workspace_has_no_home(tmp_path: Path):
    home = resolve_agent_home(tmp_path)
    assert home.roots == ()
    assert home.agents_dir is None
    assert home.mcp_paths == []
    assert home.settings == {}
    assert home.load_project_tools is False


def test_agent_folder_discovered(tmp_path: Path):
    (tmp_path / ".agent" / "agents").mkdir(parents=True)
    (tmp_path / ".agent" / "skills").mkdir()
    (tmp_path / ".agent" / "tools").mkdir()
    (tmp_path / ".agent" / "mcp.json").write_text("{}", encoding="utf-8")

    home = resolve_agent_home(tmp_path)
    assert home.roots == (tmp_path / ".agent",)
    assert home.agents_dir == tmp_path / ".agent" / "agents"
    assert home.skills_dirs == [tmp_path / ".agent" / "skills"]
    assert home.tools_dirs == [tmp_path / ".agent" / "tools"]
    assert home.mcp_paths == [str(tmp_path / ".agent" / "mcp.json")]


def test_garuda_is_backcompat_alias(tmp_path: Path):
    (tmp_path / ".garuda" / "agents").mkdir(parents=True)
    home = resolve_agent_home(tmp_path)
    assert home.roots == (tmp_path / ".garuda",)
    assert home.agents_dir == tmp_path / ".garuda" / "agents"


def test_agent_wins_over_garuda(tmp_path: Path):
    (tmp_path / ".agent" / "agents").mkdir(parents=True)
    (tmp_path / ".garuda" / "agents").mkdir(parents=True)
    home = resolve_agent_home(tmp_path)
    # precedence order: .agent before .garuda
    assert home.roots == (tmp_path / ".agent", tmp_path / ".garuda")
    assert home.agents_dir == tmp_path / ".agent" / "agents"


def test_settings_merge_agent_overrides_garuda(tmp_path: Path):
    (tmp_path / ".agent").mkdir()
    (tmp_path / ".garuda").mkdir()
    (tmp_path / ".garuda" / "settings.yaml").write_text(
        "load_project_tools: true\nshared: from_garuda\n", encoding="utf-8"
    )
    (tmp_path / ".agent" / "settings.yaml").write_text(
        "shared: from_agent\n", encoding="utf-8"
    )
    home = resolve_agent_home(tmp_path)
    # .agent overrides the shared key; .garuda-only keys survive
    assert home.settings["shared"] == "from_agent"
    assert home.settings["load_project_tools"] is True
    assert home.load_project_tools is True


def test_mcp_paths_first_file_per_root(tmp_path: Path):
    (tmp_path / ".agent").mkdir()
    # both json and yaml present: json wins (first in _MCP_FILENAMES)
    (tmp_path / ".agent" / "mcp.json").write_text("{}", encoding="utf-8")
    (tmp_path / ".agent" / "mcp.yaml").write_text("servers: []\n", encoding="utf-8")
    home = resolve_agent_home(tmp_path)
    assert home.mcp_paths == [str(tmp_path / ".agent" / "mcp.json")]


def test_resolve_agents_dir_precedence(tmp_path: Path):
    (tmp_path / ".agent" / "agents").mkdir(parents=True)
    # explicit wins
    assert resolve_agents_dir(tmp_path, "/some/explicit") == Path("/some/explicit")
    # else the home's agents dir
    assert resolve_agents_dir(tmp_path, None) == tmp_path / ".agent" / "agents"
    # nothing configured -> None
    assert resolve_agents_dir(tmp_path / "nope", None) is None


def test_mcp_config_resolution_prefers_agent_folder(tmp_path: Path):
    from garuda.mcp.config import resolve_mcp_config_paths

    (tmp_path / ".agent").mkdir()
    (tmp_path / ".garuda").mkdir()
    (tmp_path / ".agent" / "mcp.json").write_text("{}", encoding="utf-8")
    (tmp_path / ".garuda" / "mcp.json").write_text("{}", encoding="utf-8")
    paths = resolve_mcp_config_paths(tmp_path)
    assert paths == [str(tmp_path / ".agent" / "mcp.json")]


def test_skills_discovered_from_agent_folder(tmp_path: Path):
    from garuda.agents.loader import AgentProfile, resolve_system_prompt

    skill_dir = tmp_path / ".agent" / "skills" / "greet"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: greet\ndescription: say hello\n---\nBody here.\n",
        encoding="utf-8",
    )
    profile = AgentProfile(name="t", system_prompt="BASE")
    prompt = resolve_system_prompt(profile, tmp_path)
    assert "greet" in prompt
    assert "say hello" in prompt
