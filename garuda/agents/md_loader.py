"""Load OpenCode-style agent.md profiles with YAML frontmatter."""

from pathlib import Path

import yaml

from garuda.agents.frontmatter import parse_frontmatter
from garuda.agents.loader import AgentProfile, _refuse, profile_from_mapping


def load_agent_md(path: str | Path) -> AgentProfile:
    """Parse an agent.md file into an AgentProfile.

    Front matter holds the same fields as a YAML profile (one shared parser);
    the Markdown body, when present, is the system prompt.
    """
    target = Path(path)
    try:
        meta, body = parse_frontmatter(target.read_text(encoding="utf-8"), unique_keys=True)
    except yaml.YAMLError as exc:
        _refuse(target, f"has invalid front matter: {exc}")
    return profile_from_mapping(meta, target.stem, target, system_prompt=body or None)
