"""Skill sources and per-agent selection (#163, plan task H.5)."""

import json

import pytest

from garuda.agents import inspect
from garuda.agents.loader import load_profile
from garuda.skills import sources


def _skill(root, name, body="Do the thing.", tools=None):
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    front = f"---\nname: {name}\ndescription: {name} skill\n"
    if tools:
        front += f"allowed-tools: {', '.join(tools)}\n"
    (d / "SKILL.md").write_text(front + f"---\n{body}\n")


@pytest.fixture
def ws(tmp_path, monkeypatch):
    root = tmp_path / "ws"
    (root / ".agent" / "agents").mkdir(parents=True)
    project = root / ".agent" / "skills"
    user = sources.user_skills_dir()
    packaged = tmp_path / "packaged-skills"
    for where, names in ((project, ["alpha", "shared"]), (user, ["beta", "shared"]),
                         (packaged, ["gamma", "shared"])):
        for name in names:
            _skill(where, name, body=f"{name} body from {where.name}")
    monkeypatch.setattr(sources, "packaged_skills_dir", lambda: packaged)
    return root


def _show(ws, skills_yaml):
    (ws / ".agent" / "agents" / "a.yaml").write_text(
        f"version: 1\nextends: garuda/explore\nskills: {skills_yaml}\n")
    return inspect.show("a", ws)["skills"]


@pytest.mark.parametrize("skills_yaml, selected", [
    ("{}", {"alpha": "project", "beta": "user", "gamma": "packaged", "shared": "project"}),
    ("{include: null}", {"alpha": "project", "beta": "user", "gamma": "packaged",
                         "shared": "project"}),
    ("{include: []}", {}),
    ("{include: [beta, shared]}", {"beta": "user", "shared": "project"}),
    ("{exclude: [alpha, gamma]}", {"beta": "user", "shared": "project"}),
    ("{from: [user, packaged]}", {"beta": "user", "gamma": "packaged", "shared": "user"}),
    ("{from: [packaged]}", {"gamma": "packaged", "shared": "packaged"}),
    ("{from: [], include: null}", {}),
])
def test_precedence_and_filters(ws, skills_yaml, selected):
    shown = _show(ws, skills_yaml)
    assert {s["name"]: s["source"] for s in shown["selected"]} == selected


def test_shadowed_copies_are_listed(ws):
    shadowed = _show(ws, "{}")["shadowed"]
    assert {(s["name"], s["source"], s["by"]) for s in shadowed} == {
        ("shared", "user", "project"), ("shared", "packaged", "project")}


@pytest.mark.parametrize("load, expect, absent", [
    ("index", "- alpha (", "alpha body from skills"),
    ("full", "alpha body from skills", "To use a skill, read its file"),
])
def test_index_or_full_bodies_in_the_prompt(ws, load, expect, absent):
    (ws / ".agent" / "agents" / "a.yaml").write_text(
        f"version: 1\nextends: garuda/explore\nskills: {{include: [alpha], load: {load}}}\n")
    data = inspect.prompt("a", ws)
    (section,) = [s for s in data["sections"] if s["section"] == "skills"]
    assert expect in section["text"] and absent not in section["text"]


def test_the_advisory_wording_stays(ws):
    _skill(ws / ".agent" / "skills", "careful", tools=["read_file"])
    (ws / ".agent" / "agents" / "a.yaml").write_text(
        "version: 1\nextends: garuda/explore\nskills: {include: [careful]}\n")
    text = "".join(s["text"] for s in inspect.prompt("a", ws)["sections"])
    assert "[tools: read_file]" in text and "restrict yourself to those tools" in text


def test_an_unknown_include_refuses_in_version_1_and_warns_in_legacy(ws, monkeypatch, capsys,
                                                                      caplog):
    from tests.test_runtime_cli import _main

    (ws / ".agent" / "agents" / "v1.yaml").write_text(
        "version: 1\nextends: garuda/explore\nskills: {include: [ghost]}\n")
    code, out = _main(monkeypatch, capsys, "agent", "check", "v1", "--workspace", str(ws))
    assert code == 1 and "agent.unknown_skill" in out

    (ws / ".agent" / "agents" / "old.yaml").write_text("skills: [ghost]\ntools: [read_file]\n")
    profile = load_profile("old", extra_dir=ws / ".agent" / "agents")
    from garuda.agents.loader import resolve_system_prompt

    resolve_system_prompt(profile, ws)
    assert "skill 'ghost' is not available" in caplog.text


def test_a_skill_naming_a_tool_the_agent_lacks_is_reported(ws, monkeypatch, capsys):
    from tests.test_runtime_cli import _main

    _skill(ws / ".agent" / "skills", "deploy", tools=["bash", "web_fetch"])
    (ws / ".agent" / "agents" / "a.yaml").write_text(
        "version: 1\nextends: garuda/explore\nskills: {include: [deploy]}\n"
        "tools: {preset: none, add: [read_file, bash]}\n")
    code, out = _main(monkeypatch, capsys, "agent", "check", "a", "--json",
                      "--workspace", str(ws))
    codes = {d["code"]: d for d in json.loads(out)}
    assert code == 0 and "skill.tool_not_granted" in codes
    assert "web_fetch" in codes["skill.tool_not_granted"]["message"]


def test_legacy_profiles_keep_their_old_sources(ws):
    selection = sources.select(load_profile("explore"), ws)
    assert {s.name: selection.sources[s.name] for s in selection.skills} == {
        "alpha": "project", "shared": "project"}  # no user or packaged skills


def test_skill_files_never_escape_their_source(ws, tmp_path):
    outside = tmp_path / "outside"
    _skill(outside, "evil", body="exfiltrate")
    (ws / ".agent" / "skills" / "evil").symlink_to(outside / "evil")
    (ws / ".agent" / "skills" / "linked").mkdir()
    (ws / ".agent" / "skills" / "linked" / "SKILL.md").symlink_to(outside / "evil" / "SKILL.md")
    names = {s["name"] for s in _show(ws, "{}")["selected"]}
    assert "evil" not in names
