"""Profile loading no longer changes what an agent runs with silently (#146).

1. ``skills_dirs`` replaced the system prompt with the workspace path.
2. ``agent.md`` front matter dropped five fields (``reasoning_effort``,
   ``thinking_budget_tokens``, ``enable_acceptance_contract``, ``model_binding``,
   ``collection``) while still recording them as declared.
3. Unknown keys and tool names vanished without a word; duplicate keys and invalid
   permission values were accepted.
"""

import logging
from importlib import resources
from pathlib import Path

import pytest
import yaml

from garuda.agents.loader import AgentProfile, list_profiles, load_profile, resolve_system_prompt
from garuda.agents.setup import prepare_agent_run
from garuda.model.config import ConfigError
from garuda.model.script_model import ScriptModel


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("GARUDA_GLOBAL_SETTINGS", str(tmp_path / "home" / "settings.yaml"))


def _skill(root: Path, name: str) -> None:
    (root / name).mkdir(parents=True)
    (root / name / "SKILL.md").write_text(f"---\nname: {name}\ndescription: the {name} skill\n---\nbody\n")


def test_skills_dirs_keep_the_profiles_instructions(tmp_path):
    _skill(tmp_path / "skills", "release-notes")
    profile = AgentProfile(name="x", system_prompt="MY INSTRUCTIONS", skills_dirs=["skills"])

    prompt = resolve_system_prompt(profile, tmp_path)

    assert prompt.startswith("MY INSTRUCTIONS")
    assert "release-notes" in prompt


def test_an_empty_skills_dir_returns_the_instructions_as_text(tmp_path):
    (tmp_path / "skills").mkdir()
    profile = AgentProfile(name="x", system_prompt="MY INSTRUCTIONS", skills_dirs=["skills"])

    prompt = resolve_system_prompt(profile, tmp_path)

    assert isinstance(prompt, str)
    assert prompt == "MY INSTRUCTIONS"


async def test_agent_md_fields_reach_the_run(tmp_path):
    agents = tmp_path / ".agent" / "agents"
    agents.mkdir(parents=True)
    (agents / "careful.md").write_text(
        "---\nname: careful\nmode: eval\nreasoning_effort: high\n"
        "enable_acceptance_contract: false\n---\nYou are careful.\n"
    )

    prepared = await prepare_agent_run("careful", workspace=str(tmp_path), model=ScriptModel([]))

    assert prepared.config.reasoning_effort == "high"
    assert prepared.config.enable_acceptance_contract is False
    assert prepared.config.system_prompt.startswith("You are careful.")


# Hand-written non-default values: the expectation does not come from the parser.
NON_DEFAULTS = {
    "description": "d",
    "permission_mode": "readonly",
    "mode": "eval",
    "tools": ["bash", "read_file"],
    "tool_rules": {"bash": "ask"},
    "path_rules": {"deny": [".env"]},
    "bash_rules": {"allow_prefixes": ["git status"]},
    "max_turns": 7,
    "enable_tmux": False,
    "marker_polling": False,
    "enable_three_step_summary": False,
    "enable_acceptance_contract": False,
    "max_context_tokens": 64000,
    "proactive_summarize_threshold": 1234,
    "max_output_bytes": 4096,
    "reserved_output_tokens": 9000,
    "context_safety_margin_tokens": 1500,
    "enable_request_preflight": False,
    "enable_adaptive_output": False,
    "min_output_bytes": 1024,
    "max_tokens": 2048,
    "enable_working_state_card": False,
    "workspace_kind": "docker",
    "docker_image": "python:3.12",
    "mcp_config_path": "custom-mcp.json",
    "mcp_servers": ["docs"],
    "skills": ["release-notes"],
    "skills_dirs": ["more-skills"],
    "subagent": True,
    "reasoning_effort": "high",
    "thinking_budget_tokens": 4096,
    "model_binding": "cheap",
    "collection": {"enabled": False},
    "condenser": "recent_window",
    "deadline_sec": 90.0,
    "enable_verifier": False,
    "docker_network": False,
    "docker_memory": "1g",
    "docker_cpus": 1.0,
    "memory_user": True,
    "memory_project": ["CONTRIBUTING.md"],
    "memory_project_mode": "all",
    "memory_max_chars": 100,
    "memory_max_total_chars": 9000,
    "memory_context_pack": True,
    "memory_notes": "propose",
    "skills_from": ["project", "user"],
    "skills_exclude": ["old-skill"],
    "skills_load": "full",
    "tool_options": {"bash": {"timeout_sec": 30}},
    "subagents": ["explore"],
}


def test_the_fixture_covers_every_authorable_field():
    from dataclasses import fields

    authorable = {f.name for f in fields(AgentProfile)} - {"name", "system_prompt", "declared_fields", "source_path",
                                                      "spec_version", "tools_removed",
                                                      "output_schema", "spec_digest"}
    assert authorable == set(NON_DEFAULTS)


@pytest.mark.parametrize("field_name", sorted(NON_DEFAULTS))
def test_yaml_and_agent_md_read_every_field_the_same(tmp_path, field_name):
    value = NON_DEFAULTS[field_name]
    body = yaml.safe_dump({"name": "p", field_name: value})
    (tmp_path / "p.yaml").write_text(body)
    md_dir = tmp_path / "md"
    md_dir.mkdir()
    (md_dir / "p.md").write_text(f"---\n{body}---\nprompt\n")

    from_yaml = load_profile("p", extra_dir=tmp_path)
    from_md = load_profile("p", extra_dir=md_dir)

    assert getattr(from_yaml, field_name) == value
    assert getattr(from_md, field_name) == value
    assert field_name in from_yaml.declared_fields
    assert field_name in from_md.declared_fields


def test_an_unknown_key_warns_once_naming_the_file(tmp_path, caplog):
    (tmp_path / "typo.yaml").write_text("name: typo\nsystemprompt: hello\n")

    with caplog.at_level(logging.WARNING, logger="garuda.agents.loader"):
        profile = load_profile("typo", extra_dir=tmp_path)

    warnings = [r.getMessage() for r in caplog.records if "systemprompt" in r.getMessage()]
    assert len(warnings) == 1
    assert "typo.yaml" in warnings[0]
    assert "system_prompt" in warnings[0]  # the suggestion
    assert profile.system_prompt is None


async def test_an_unknown_tool_warns_once_naming_the_file(tmp_path, caplog):
    agents = tmp_path / ".agent" / "agents"
    agents.mkdir(parents=True)
    (agents / "t.yaml").write_text("name: t\ntools: [bash, red_file, task_complete]\n")

    with caplog.at_level(logging.WARNING, logger="garuda.agents.setup"):
        await prepare_agent_run("t", workspace=str(tmp_path), model=ScriptModel([]))

    warnings = [r.getMessage() for r in caplog.records if "red_file" in r.getMessage()]
    assert len(warnings) == 1
    assert "t.yaml" in warnings[0]


@pytest.mark.parametrize(
    "body",
    [
        "name: d\npermission_mode: readonly\npermission_mode: yolo\n",
        "name: d\npermission_mode: superuser\n",
        "name: d\ntool_rules:\n  bash: maybe\n",
        "name: d\npath_rules:\n  allow: ['*']\n",
        "name: d\nbash_rules:\n  deny: 'rm'\n",
    ],
    ids=["duplicate-key", "unknown-mode", "bad-tool-rule", "bad-path-rule-key", "rule-not-a-list"],
)
@pytest.mark.parametrize("fmt", ["yaml", "md"])
def test_an_ambiguous_security_policy_refuses(tmp_path, body, fmt):
    if fmt == "yaml":
        (tmp_path / "d.yaml").write_text(body)
    else:
        (tmp_path / "d.md").write_text(f"---\n{body}---\nprompt\n")

    with pytest.raises(ConfigError):
        load_profile("d", extra_dir=tmp_path)


@pytest.mark.parametrize("name", list_profiles())
def test_packaged_profiles_load_cleanly_and_as_written(name, caplog):
    with caplog.at_level(logging.WARNING, logger="garuda.agents.loader"):
        profile = load_profile(name)
    assert not caplog.records
    defaults = Path(resources.files("garuda.agents")) / "defaults"
    source = defaults / f"{name}.yaml"
    if source.exists():
        written = yaml.safe_load(source.read_text())
        if written.get("version") == 1:  # a nested definition: resolved, not a flat key map
            return
        for key, value in written.items():
            assert getattr(profile, key) == value, key
