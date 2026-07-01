"""Parse YAML frontmatter from markdown agent and skill files."""

import re
from typing import Any

import yaml

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Return (frontmatter dict, body markdown) from a markdown file."""
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return {}, text.strip()
    meta = yaml.safe_load(match.group(1)) or {}
    body = text[match.end() :].strip()
    return meta, body
