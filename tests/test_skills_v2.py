"""Tests for skills progressive disclosure: index injection and allowed-tools."""

from garuda.skills.loader import discover_skills, format_skills_prompt, load_skill


def _write_skill(tmp_path, name, description="A demo skill", body="Detailed body instructions.", extra_meta=""):
    skill_dir = tmp_path / name
    skill_dir.mkdir(parents=True)
    path = skill_dir / "SKILL.md"
    path.write_text(
        f"---\nname: {name}\ndescription: {description}\n{extra_meta}---\n\n{body}\n",
        encoding="utf-8",
    )
    return path


def test_index_injection_omits_bodies(tmp_path):
    path = _write_skill(tmp_path, "pdf-skill", body="SECRET-BODY-CONTENT extract text carefully.")
    skills = discover_skills(tmp_path)
    prompt = format_skills_prompt(skills)
    assert prompt.startswith("## Available skills")
    assert f"- pdf-skill ({path}): A demo skill" in prompt
    assert "read its file with read_file(<path>)" in prompt
    assert "SECRET-BODY-CONTENT" not in prompt


def test_full_body_escape_hatch(tmp_path):
    _write_skill(tmp_path, "pdf-skill", body="SECRET-BODY-CONTENT extract text carefully.")
    skills = discover_skills(tmp_path)
    prompt = format_skills_prompt(skills, full_body=True)
    assert "SECRET-BODY-CONTENT" in prompt
    assert "### Skill: pdf-skill" in prompt


def test_empty_skills_prompt():
    assert format_skills_prompt([]) == ""


def test_allowed_tools_list_frontmatter(tmp_path):
    path = _write_skill(
        tmp_path,
        "guarded",
        extra_meta="allowed-tools:\n  - read_file\n  - bash\n",
    )
    skill = load_skill(path)
    assert skill.allowed_tools == ["read_file", "bash"]


def test_allowed_tools_comma_string_frontmatter(tmp_path):
    path = _write_skill(tmp_path, "guarded", extra_meta="allowed-tools: read_file, bash\n")
    skill = load_skill(path)
    assert skill.allowed_tools == ["read_file", "bash"]


def test_allowed_tools_absent(tmp_path):
    path = _write_skill(tmp_path, "plain")
    skill = load_skill(path)
    assert skill.allowed_tools is None
