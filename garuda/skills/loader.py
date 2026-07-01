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

    def to_prompt_block(self) -> str:
        return f"### Skill: {self.name}\n{self.description}\n\n{self.body}"


def load_skill(path: str | Path) -> Skill:
    """Load a single SKILL.md or skill markdown file."""
    target = Path(path)
    meta, body = parse_frontmatter(target.read_text(encoding="utf-8"))
    return Skill(
        name=meta.get("name", target.parent.name),
        description=meta.get("description", ""),
        body=body,
        path=target,
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


def format_skills_prompt(skills: list[Skill]) -> str:
    """Format loaded skills for injection into the system prompt."""
    if not skills:
        return ""
    blocks = [skill.to_prompt_block() for skill in skills]
    return "## Available Skills\n\n" + "\n\n---\n\n".join(blocks)
