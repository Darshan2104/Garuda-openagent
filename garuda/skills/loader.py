"""Universal SKILL.md loader (Anthropic / OpenCode compatible format)."""

from dataclasses import dataclass
from pathlib import Path

from garuda.agents.frontmatter import parse_frontmatter


@dataclass
class Skill:
    name: str
    description: str
    body: str
    path: Path | None = None
    allowed_tools: list[str] | None = None

    def to_prompt_block(self) -> str:
        return f"### Skill: {self.name}\n{self.description}\n\n{self.body}"

    def to_index_line(self) -> str:
        location = str(self.path) if self.path else "(no file)"
        tools = f" [tools: {', '.join(self.allowed_tools)}]" if self.allowed_tools else ""
        return f"- {self.name} ({location}){tools}: {self.description}"


def _parse_allowed_tools(value: object) -> list[str] | None:
    """Normalize the optional `allowed-tools:` frontmatter into a list of names."""
    if value is None:
        return None
    if isinstance(value, str):
        items = [item.strip() for item in value.split(",")]
    elif isinstance(value, (list, tuple)):
        items = [str(item).strip() for item in value]
    else:
        return None
    return [item for item in items if item] or None


def load_skill(path: str | Path) -> Skill:
    """Load a single SKILL.md or skill markdown file."""
    target = Path(path)
    meta, body = parse_frontmatter(target.read_text(encoding="utf-8"))
    return Skill(
        name=meta.get("name", target.parent.name),
        description=meta.get("description", ""),
        body=body,
        path=target,
        allowed_tools=_parse_allowed_tools(meta.get("allowed-tools")),
    )


def discover_skills(*directories: str | Path) -> list[Skill]:
    """Discover SKILL.md files under standard locations."""
    skills: list[Skill] = []
    seen: set[str] = set()
    for directory in directories:
        root = Path(directory)
        if not root.exists():
            continue
        patterns = ["SKILL.md", "skill.md", "**/SKILL.md", "**/skill.md"]
        for pattern in patterns:
            for path in root.glob(pattern):
                if not path.is_file():
                    continue
                skill = load_skill(path)
                if skill.name in seen:
                    continue
                seen.add(skill.name)
                skills.append(skill)
    return sorted(skills, key=lambda s: s.name)


def format_skills_prompt(skills: list[Skill], full_body: bool = False) -> str:
    """Format loaded skills for injection into the system prompt.

    By default only a lightweight index is injected (progressive disclosure):
    one line per skill with its name, file path, and description. The agent is
    instructed to read the skill file on demand. Pass full_body=True to inject
    every skill's complete body (legacy behavior).
    """
    if not skills:
        return ""
    if full_body:
        blocks = [skill.to_prompt_block() for skill in skills]
        return "## Available Skills\n\n" + "\n\n---\n\n".join(blocks)
    lines = [skill.to_index_line() for skill in skills]
    return (
        "## Available skills\n"
        + "\n".join(lines)
        + "\n\nTo use a skill, read its file with read_file(<path>) first and follow its "
        "instructions. When a skill lists specific tools in [tools: ...], restrict yourself "
        "to those tools while carrying out that skill."
    )
