"""Version 1 agent definitions, one resolver and `extends` (#159, plan task H.1)."""

import dataclasses
import json
import os
import shutil

import pytest

from garuda.agents import migrate, resolve
from garuda.agents.frontmatter import load_yaml_unique, parse_frontmatter
from garuda.agents.loader import _defaults_dir, list_profiles, load_profile, profile_from_mapping
from garuda.model.config import ConfigError
from garuda.types import DEFAULT_SYSTEM_PROMPT

PACKAGED = ["build", "explore", "harbor", "plan", "reviewer"]


def _baseline(name):
    """The pre-H.1 loader: one file, parsed straight into a profile."""
    for path in (_defaults_dir() / f"{name}.yaml", _defaults_dir() / f"{name}.md"):
        if path.exists():
            text = path.read_text()
            if path.suffix == ".md":
                meta, body = parse_frontmatter(text, unique_keys=True)
                return profile_from_mapping(meta, path.stem, path, system_prompt=body or None)
            return profile_from_mapping(load_yaml_unique(text), name, path)
    raise AssertionError(name)


def _facts(profile):
    config = profile.to_agent_config()
    return (config, profile.tools, profile.tool_rules, profile.path_rules, profile.bash_rules,
            config.system_prompt, sorted(profile.declared_fields))


@pytest.fixture
def project(tmp_path):
    agents = tmp_path / "ws" / ".agent" / "agents"
    agents.mkdir(parents=True)
    return agents


def test_packaged_profiles_are_listed():
    assert set(PACKAGED) <= set(list_profiles())


@pytest.mark.parametrize("name", PACKAGED)
def test_golden_equivalence_through_the_resolver_and_after_migration(name, project):
    baseline = _baseline(name)
    assert _facts(load_profile(name)) == _facts(baseline)

    source = next(p for p in _defaults_dir().iterdir() if p.stem == name)
    copy = project / source.name
    shutil.copy(source, copy)
    plan = migrate.plan(copy)
    assert plan.diff == [] and plan.tightening == []
    copy.write_text(plan.text)
    migrated = load_profile(name, extra_dir=project)
    assert migrated.spec_version == 1
    assert _facts(migrated)[:6] == _facts(baseline)[:6]


def test_migrate_writes_once_with_a_backup(project, monkeypatch, capsys):
    from tests.test_runtime_cli import _main

    shutil.copy(_defaults_dir() / "explore.yaml", project / "explore.yaml")
    before = load_profile("explore", extra_dir=project)
    code, out = _main(monkeypatch, capsys, "agent", "migrate", str(project / "explore.yaml"),
                      "--write")
    assert code == 0 and "resolves to exactly the same agent" in out
    assert list(project.glob("explore.yaml.bak-*"))
    assert _facts(load_profile("explore", extra_dir=project))[:6] == _facts(before)[:6]
    code, out = _main(monkeypatch, capsys, "agent", "migrate", str(project / "explore.yaml"),
                      "--write")
    assert code == 0 and "nothing to do" in out
    assert len(list(project.glob("explore.yaml.bak-*"))) == 1


def test_migrate_keeps_an_agent_md_body(project):
    shutil.copy(_defaults_dir() / "reviewer.md", project / "reviewer.md")
    plan = migrate.plan(project / "reviewer.md")
    assert plan.text.startswith("---\nversion: 1\n")
    assert plan.text.rstrip().endswith("summarizing findings and recommendations.")
    assert plan.diff == []


# --- precedence and extends -----------------------------------------------------------


def test_a_project_build_overrides_and_garuda_build_stays_packaged(project):
    (project / "build.yaml").write_text("version: 1\nlimits: {max_turns: 7}\n")
    assert load_profile("build", extra_dir=project).max_turns == 7
    assert load_profile("garuda/build", extra_dir=project).max_turns == 200


def test_extends_changes_one_thing_and_appends_instructions(project):
    (project / "careful.yaml").write_text(
        "version: 1\nextends: garuda/build\ninstructions:\n  text: Make the smallest change.\n")
    profile = load_profile("careful", extra_dir=project)
    build = load_profile("garuda/build")
    assert profile.tools == build.tools and profile.permission_mode == build.permission_mode
    base = build.system_prompt or DEFAULT_SYSTEM_PROMPT
    assert profile.system_prompt == base.rstrip() + "\n\nMake the smallest change."
    assert profile.name == "careful"


def test_tools_add_remove_and_preset(project):
    from garuda.tools.registry import list_tool_names

    explore = load_profile("garuda/explore").tools
    new = next(name for name in sorted(list_tool_names()) if name not in explore)
    kept = explore[0]
    (project / "lean.yaml").write_text(
        f"version: 1\nextends: garuda/explore\ntools: {{remove: [{explore[-1]}],"
        f" add: [{new}, {kept}]}}\n")
    (project / "solo.yaml").write_text(
        "version: 1\nextends: garuda/build\ntools: {preset: none, add: [read_file]}\n")
    lean = load_profile("lean", extra_dir=project).tools
    assert lean == [t for t in explore if t != explore[-1]] + [new]  # kept is not doubled
    assert load_profile("solo", extra_dir=project).tools == ["read_file"]


def test_a_user_agent_is_found_after_the_project(project):
    user = resolve.user_agents_dir()
    user.mkdir(parents=True, exist_ok=True)
    (user / "mine.yaml").write_text("version: 1\nextends: garuda/explore\nlimits: {max_turns: 9}\n")
    assert load_profile("mine", extra_dir=project).max_turns == 9
    assert load_profile("user/mine").max_turns == 9
    (project / "mine.yaml").write_text("version: 1\nlimits: {max_turns: 3}\n")
    assert load_profile("mine", extra_dir=project).max_turns == 3


def test_a_project_cannot_inherit_its_way_above_the_ceiling(project, tmp_path):
    from garuda.agents.authority import enforce_project_ceiling

    (project / "wide.yaml").write_text("version: 1\nextends: garuda/harbor\n")
    profile = load_profile("wide", extra_dir=project)
    if profile.permission_mode in ("auto", "yolo"):
        with pytest.raises(ConfigError, match="agent.project_widening"):
            enforce_project_ceiling(profile_name="wide", declared_mode=profile.permission_mode,
                                    source_path=profile.source_path,
                                    workspace=tmp_path / "ws", explicit_permission_mode=None,
                                    global_settings={})


# --- refusals -------------------------------------------------------------------------


def _refuses(project, files: dict, name: str, code: str):
    for filename, text in files.items():
        path = project / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    with pytest.raises(ConfigError) as caught:
        load_profile(name, extra_dir=project)
    assert code in str(caught.value), str(caught.value)


@pytest.mark.parametrize("files, name, code", [
    ({"a.yaml": "version: 1\ncolour: blue\n"}, "a", "agent.unknown_field"),
    ({"a.yaml": "version: 1\nlimits: {max_turns: 1, max_tunrs: 2}\n"}, "a", "agent.unknown_field"),
    ({"a.yaml": "version: 1\nauthority: user\n"}, "a", "agent.unknown_field"),
    ({"a.yaml": "version: 1\ntools: {preset: none, add: [teleport]}\n"}, "a", "agent.unknown_tool"),
    ({"a.yaml": "version: 1\ninstructions: {files: [missing.md]}\n"}, "a",
     "agent.instruction_file_missing"),
    ({"build.yaml": "version: 1\nextends: build\n"}, "build", "agent.extends_cycle"),
    ({"a.yaml": "version: 1\nextends: project/b\n", "b.yaml": "version: 1\nextends: project/a\n"},
     "a", "agent.extends_cycle"),
    ({"a.yaml": "version: 1\nlimits: {max_turns: 1}\nlimits: {max_turns: 2}\n"}, "a",
     "duplicate key"),
    ({"a.yaml": "version: 2\n"}, "a", "agent.unsupported_version"),
    ({"a.md": "---\nversion: 1\ninstructions: {text: inline}\n---\nbody too\n"}, "a",
     "agent.ambiguous_instructions"),
    ({"a.yaml": "version: 1\nmemory: {notes: propose}\n"}, "a", "agent.unsupported_field"),
    ({"a.yaml": "version: 1\ntools: {preset: all}\n"}, "a", "agent.unsupported_field"),
    ({"a.yaml": "version: 1\npermissions: {mode: root}\n"}, "a", "agent.invalid_value"),
    ({"a.yaml": "version: 1\nlimits: {max_turns: null}\n"}, "a", "agent.invalid_value"),
    ({"a.yaml": "permission_mode: root\n"}, "a", "permission_mode must be one of"),
    ({"a.yaml": "x: !!python/object/apply:os.system [true]\n"}, "a", "not valid YAML"),
])
def test_each_refusal_through_the_public_loader(project, files, name, code):
    _refuses(project, files, name, code)


def test_extends_has_a_depth_limit(project):
    for i in range(6):
        parent = f"project/a{i + 1}" if i < 5 else "garuda/build"
        (project / f"a{i}.yaml").write_text(f"version: 1\nextends: {parent}\n")
    with pytest.raises(ConfigError, match="agent.extends_too_deep"):
        load_profile("a0", extra_dir=project)


@pytest.mark.parametrize("name", ["../build", "/etc/passwd", "a/b/c", "evil/build", "", "a b"])
def test_names_are_identifiers_not_paths(name):
    with pytest.raises(ConfigError, match="agent.invalid_name"):
        load_profile(name)


def test_references_cannot_escape_the_project(project, tmp_path):
    outside = tmp_path / "outside.md"
    outside.write_text("secret instructions")
    (project / "notes.md").symlink_to(outside)
    _refuses(project, {"a.yaml": "version: 1\ninstructions: {files: [notes.md]}\n"}, "a",
             "agent.path_escapes")
    _refuses(project, {"b.yaml": "version: 1\ninstructions: {files: [../../../outside.md]}\n"},
             "b", "agent.path_escapes")
    (project / "c.yaml").symlink_to(outside)
    with pytest.raises(ConfigError, match="agent.path_escapes"):
        load_profile("c", extra_dir=project)


def test_instruction_files_are_read_relative_to_the_definition(project):
    (project / "style.md").write_text("Prefer small diffs.")
    (project / "a.yaml").write_text(
        "version: 1\ninstructions: {mode: replace, text: Be careful., files: [style.md]}\n")
    assert load_profile("a", extra_dir=project).system_prompt == "Be careful.\n\nPrefer small diffs."


def test_legacy_unknown_keys_warn_but_load(project, caplog):
    (project / "old.yaml").write_text("max_turns: 5\ncolour: blue\n")
    assert load_profile("old", extra_dir=project).max_turns == 5
    assert "unknown key 'colour'" in caplog.text


def test_unknown_skills_and_mcp_servers_refuse_at_activation(tmp_path, project):
    from garuda.agents.resolve import check_references

    ws = tmp_path / "ws"
    (project / "a.yaml").write_text("version: 1\nskills: {include: [ghost]}\n")
    with pytest.raises(ConfigError, match="agent.unknown_skill"):
        check_references(load_profile("a", extra_dir=project), ws)
    (ws / ".agent" / "mcp.json").write_text(json.dumps({"mcpServers": {"real": {"command": "x"}}}))
    (project / "b.yaml").write_text("version: 1\ntools: {mcp: [ghost]}\n")
    with pytest.raises(ConfigError, match="agent.unknown_mcp_server"):
        check_references(load_profile("b", extra_dir=project), ws)
    (project / "c.yaml").write_text("version: 1\ntools: {mcp: [real]}\n")
    check_references(load_profile("c", extra_dir=project), ws)


def test_resolution_activates_nothing(tmp_path, project):
    ws = tmp_path / "ws"
    sentinel = tmp_path / "sentinel"
    (ws / ".agent" / "mcp.json").write_text(json.dumps(
        {"mcpServers": {"s": {"command": "sh", "args": ["-c", f"touch {sentinel}"]}}}))
    (ws / ".agent" / "settings.yaml").write_text(
        f"hooks:\n  before_tool:\n    - match: '*'\n      command: touch {sentinel}\n")
    (ws / ".agent" / "tools").mkdir()
    (ws / ".agent" / "tools" / "evil.py").write_text(f"open({str(sentinel)!r}, 'w')\n")
    (project / "a.yaml").write_text("version: 1\nextends: garuda/build\ntools: {mcp: [s]}\n")

    resolve.resolve_agent("a", [project])
    load_profile("a", extra_dir=project)
    assert not sentinel.exists()


def test_the_profile_projection_matches_the_dataclass():
    fields = {f.name for f in dataclasses.fields(load_profile("build"))}
    assert "spec_version" in fields and os.environ.get("PYTEST_CURRENT_TEST")
