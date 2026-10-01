"""Parse YAML frontmatter from markdown agent and skill files."""

import re
from typing import Any

import yaml

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


class _UniqueKeyLoader(yaml.SafeLoader):
    """A safe loader that refuses a mapping with the same key twice.

    Plain ``safe_load`` keeps the last value silently, so a profile that says
    ``permission_mode: readonly`` and later ``permission_mode: yolo`` would run
    with whichever came last and nobody would know.
    """


def _construct_unique_mapping(loader, node, deep=False):
    seen = set()
    for key_node, _ in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in seen:
            raise yaml.constructor.ConstructorError(
                None, None, f"duplicate key {key!r}", key_node.start_mark
            )
        seen.add(key)
    return loader.construct_mapping(node, deep=deep)


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique_mapping
)


def load_yaml_unique(text: str) -> Any:
    """``yaml.safe_load`` that raises on duplicate mapping keys."""
    return yaml.load(text, Loader=_UniqueKeyLoader)  # noqa: S506 - SafeLoader subclass


def parse_frontmatter(text: str, *, unique_keys: bool = False) -> tuple[dict[str, Any], str]:
    """Return (frontmatter dict, body markdown) from a markdown file.

    ``unique_keys`` refuses duplicate keys; profiles use it, skills keep the
    lenient behavior.
    """
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return {}, text.strip()
    raw = match.group(1)
    meta = (load_yaml_unique(raw) if unique_keys else yaml.safe_load(raw)) or {}
    body = text[match.end() :].strip()
    return meta, body
