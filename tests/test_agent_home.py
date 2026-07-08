"""§1: the `.agent/` project-home resolver and its integration points."""

from pathlib import Path

from garuda.config.agent_home import (
    global_home_dir,
    resolve_agent_home,
    resolve_agents_dir,
    resolve_agents_dirs,
)


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
    # .agent overrides the shared key; .garuda-only keys survive in the raw merge...
    assert home.settings["shared"] == "from_agent"
    assert home.settings["load_project_tools"] is True
    # ...but load_project_tools is a trust anchor: it's read from GLOBAL settings
    # only, so a project's own settings.yaml can never self-enable it (a cloned
    # repo shipping this key must not be able to authorize its own code to run).
    assert home.load_project_tools is False


def test_load_project_tools_ignores_project_settings_but_honors_global(
    tmp_path: Path, monkeypatch
):
    (tmp_path / ".agent").mkdir()
    (tmp_path / ".agent" / "settings.yaml").write_text(
        "load_project_tools: true\n", encoding="utf-8"
    )
    # Project alone can't enable it...
    assert resolve_agent_home(tmp_path).load_project_tools is False

    # ...but the user's global settings.yaml can.
    global_settings = tmp_path.parent / "global-settings.yaml"
    global_settings.write_text("load_project_tools: true\n", encoding="utf-8")
    monkeypatch.setenv("GARUDA_GLOBAL_SETTINGS", str(global_settings))
    assert resolve_agent_home(tmp_path).load_project_tools is True


def test_trust_project_hooks_ignores_project_settings_but_honors_global(
    tmp_path: Path, monkeypatch
):
    (tmp_path / ".agent").mkdir()
    (tmp_path / ".agent" / "settings.yaml").write_text(
        "trust_project_hooks: true\n", encoding="utf-8"
    )
    assert resolve_agent_home(tmp_path).trust_project_hooks is False

    global_settings = tmp_path.parent / "global-settings2.yaml"
    global_settings.write_text("trust_project_hooks: true\n", encoding="utf-8")
    monkeypatch.setenv("GARUDA_GLOBAL_SETTINGS", str(global_settings))
    assert resolve_agent_home(tmp_path).trust_project_hooks is True


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


def _write_skill(root: Path, name: str):
    d = root / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: from {root.parent.name}\n---\nbody\n",
        encoding="utf-8",
    )


def test_skills_merge_across_roots_agent_wins(tmp_path: Path):
    from garuda.agents.loader import AgentProfile, resolve_system_prompt

    # same skill name in both roots; .agent should win, .garuda-only skill still shows
    _write_skill(tmp_path / ".agent" / "skills", "shared")
    _write_skill(tmp_path / ".garuda" / "skills", "shared")
    _write_skill(tmp_path / ".garuda" / "skills", "legacy_only")
    prompt = resolve_system_prompt(AgentProfile(name="t", system_prompt="BASE"), tmp_path)
    assert "from .agent" in prompt  # .agent's "shared" won (discover dedups by name, first wins)
    assert "legacy_only" in prompt  # a .garuda-only skill is still discovered


# --- profiles: same standard method (search all roots, .agent wins) -------------


def _write_profile(root: Path, name: str, mode: str):
    root.mkdir(parents=True, exist_ok=True)
    (root / f"{name}.yaml").write_text(f"name: {name}\nmode: {mode}\n", encoding="utf-8")


def test_agents_dirs_lists_both_roots_in_order(tmp_path: Path):
    (tmp_path / ".agent" / "agents").mkdir(parents=True)
    (tmp_path / ".garuda" / "agents").mkdir(parents=True)
    home = resolve_agent_home(tmp_path)
    assert home.agents_dirs == [
        tmp_path / ".agent" / "agents",
        tmp_path / ".garuda" / "agents",
    ]


def test_resolve_agents_dirs_precedence(tmp_path: Path):
    (tmp_path / ".agent" / "agents").mkdir(parents=True)
    (tmp_path / ".garuda" / "agents").mkdir(parents=True)
    # explicit (single or list) wins as-is
    assert resolve_agents_dirs(tmp_path, "/x") == [Path("/x")]
    assert resolve_agents_dirs(tmp_path, ["/x", "/y"]) == [Path("/x"), Path("/y")]
    # else both home roots, .agent first
    assert resolve_agents_dirs(tmp_path, None) == [
        tmp_path / ".agent" / "agents",
        tmp_path / ".garuda" / "agents",
    ]


def test_load_profile_searches_both_roots_agent_wins(tmp_path: Path):
    from garuda.agents.loader import load_profile

    _write_profile(tmp_path / ".agent" / "agents", "myrig", "rigorous")
    _write_profile(tmp_path / ".garuda" / "agents", "myrig", "standard")  # shadowed
    _write_profile(tmp_path / ".garuda" / "agents", "legacy", "standard")

    dirs = resolve_agents_dirs(tmp_path, None)
    # .agent/agents wins for a name present in both
    assert load_profile("myrig", extra_dir=dirs).mode == "rigorous"
    # a profile only in .garuda/agents is still found (same standard search)
    assert load_profile("legacy", extra_dir=dirs).mode == "standard"


def test_list_profiles_accepts_dir_list(tmp_path: Path):
    from garuda.agents.loader import list_profiles

    _write_profile(tmp_path / ".agent" / "agents", "alpha", "standard")
    _write_profile(tmp_path / ".garuda" / "agents", "beta", "standard")
    names = list_profiles(resolve_agents_dirs(tmp_path, None))
    assert {"alpha", "beta"} <= set(names)


# --- global home: ~/.agent standard, ~/.garuda back-compat ----------------------


def test_global_home_prefers_agent(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    (tmp_path / ".agent").mkdir()
    (tmp_path / ".garuda").mkdir()
    assert global_home_dir() == tmp_path / ".agent"


def test_global_home_falls_back_to_garuda(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    (tmp_path / ".garuda").mkdir()  # only legacy exists
    assert global_home_dir() == tmp_path / ".garuda"


def test_global_home_defaults_to_agent_when_neither_exists(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    assert global_home_dir() == tmp_path / ".agent"
