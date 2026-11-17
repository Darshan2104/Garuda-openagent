"""Conservative path identities for deny/ask screening on every platform."""

import os
import unicodedata


def fold_path(value: str) -> str:
    """Always fold; distinct case-sensitive host files may be over-denied."""
    return unicodedata.normalize("NFC", value).casefold()


def path_forms(path: str, root: str, cwd: str | None = None) -> tuple[set[str], set[str]]:
    """Compute lexical and physical forms once per operand, before matching."""
    absolute = os.path.normpath(os.path.join(root, cwd or "", path))
    real_root = os.path.realpath(root)
    physical = os.path.realpath(absolute)
    path, absolute, root, real_root, physical = (
        fold_path(value) for value in (path, absolute, root, real_root, physical)
    )
    forms = set()
    absolute_forms = set()
    outside = os.path.commonpath((physical, real_root)) != real_root
    for candidate, anchor in ((absolute, root), (physical, real_root)):
        if os.path.commonpath((candidate, anchor)) == anchor:
            forms.add(os.path.relpath(candidate, anchor))
            absolute_forms.add(candidate)
        elif outside:
            forms.update((path, candidate))
    return forms, forms | absolute_forms


def path_patterns(pattern: str) -> set[str]:
    """Absolute rules also screen physical aliases."""
    physical = os.path.realpath(pattern) if os.path.isabs(pattern) else pattern
    pattern = fold_path(pattern)
    patterns = {pattern, pattern.removeprefix("**/")}
    if os.path.isabs(pattern):
        patterns.add(fold_path(physical))
    patterns |= {p[:-3] for p in list(patterns) if p.endswith("/**") and len(p) > 3}
    return patterns
