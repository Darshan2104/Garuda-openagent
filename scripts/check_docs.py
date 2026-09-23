"""Documentation contract checks for issue #9 (P0.3).

Three fail-closed checks, local only — no remote fetching:

1. Broken local Markdown links: every relative ``[text](target)`` in a tracked
   ``*.md`` file must resolve to an existing path. Remote URLs, mailto links,
   and pure anchors are skipped, never fetched.
2. Nested READMEs: only the root ``README.md`` may exist. Anything else named
   ``README.md`` (case-insensitive) outside the excluded directories fails.
3. Hand-maintained test counts: release docs must not claim a specific test
   count (e.g. "1131 tests passing"). Historical records under ``docs/archive/``
   are excluded; everything else under ``README.md``, ``AGENTS.md`` and
   ``docs/`` is checked.

Exceptions must be explicit: add the path to ``ALLOWED_NESTED_READMES`` below
and get it reviewed — there is currently no exception.

Usage:
    python scripts/check_docs.py [--root PATH]
"""

from __future__ import annotations

import argparse
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]

# Directories never scanned (vendored, cached, or build output).
EXCLUDED_DIRS = frozenset(
    {
        ".git",
        ".pytest_cache",
        ".opencode",
        "node_modules",
        "__pycache__",
        ".venv",
        "venv",
        ".tox",
        "dist",
        "build",
    }
)

# Explicit, reviewed exceptions to the nested-README ban. Empty by design:
# add a POSIX path here only with reviewer approval (issue #9).
ALLOWED_NESTED_READMES: tuple[str, ...] = ()

# Historical records are exempt from the test-count ban; they describe a past
# measurement, not the current release.
TEST_COUNT_EXCLUDE_PREFIXES = ("docs/archive/",)

LINK_RE = re.compile(r"\[[^\]]*\]\(([^)]+)\)")
SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*:")
TEST_COUNT_RES = (
    re.compile(r"\b\d{3,}\s+tests?\b", re.IGNORECASE),
    re.compile(r"\b\d+\s+tests?\s+(passing|green|failing|covered|covering)\b", re.IGNORECASE),
)


def _is_excluded(path: pathlib.Path, root: pathlib.Path) -> bool:
    try:
        rel_parts = path.relative_to(root).parts
    except ValueError:
        return True
    for part in rel_parts[:-1]:
        if part in EXCLUDED_DIRS or part.endswith(".egg-info"):
            return True
    return False


def iter_markdown_files(root: pathlib.Path) -> list[pathlib.Path]:
    files = []
    for path in sorted(root.rglob("*.md")):
        if _is_excluded(path, root):
            continue
        files.append(path)
    return files


def find_nested_readmes(root: pathlib.Path) -> list[str]:
    offenders = []
    for path in iter_markdown_files(root):
        if path.name.lower() != "readme.md":
            continue
        rel = path.relative_to(root).as_posix()
        if rel == "README.md":
            continue
        if rel in ALLOWED_NESTED_READMES:
            continue
        offenders.append(rel)
    return sorted(offenders)


def _link_target_is_remote(target: str) -> bool:
    if not target or target.startswith("#"):
        return True
    return bool(SCHEME_RE.match(target))


def find_broken_local_links(root: pathlib.Path) -> list[str]:
    broken = []
    for path in iter_markdown_files(root):
        try:
            text = path.read_text(encoding="utf-8", errors="strict")
        except (OSError, UnicodeError):
            continue
        rel = path.relative_to(root).as_posix()
        for match in LINK_RE.finditer(text):
            raw = match.group(1).strip().strip("<>").strip()
            if not raw:
                continue
            # "[text](path \"title\")" — only the path matters.
            target = raw.split()[0].strip().strip("<>").strip()
            if _link_target_is_remote(target):
                continue
            target_path = target.split("#")[0].split("?")[0].strip()
            if not target_path:
                continue
            resolved = (path.parent / target_path).resolve()
            try:
                exists = resolved.exists()
            except OSError:
                exists = False
            if not exists:
                broken.append(f"{rel} -> {target}")
    return sorted(broken)


def _is_test_count_excluded(rel: str) -> bool:
    return rel.startswith(TEST_COUNT_EXCLUDE_PREFIXES)


def find_test_count_claims(root: pathlib.Path) -> list[str]:
    claims = []
    seen_lines: set[tuple[str, int]] = set()
    candidates: list[pathlib.Path] = []
    for name in ("README.md", "AGENTS.md"):
        p = root / name
        if p.is_file():
            candidates.append(p)
    docs = root / "docs"
    if docs.is_dir():
        candidates.extend(p for p in iter_markdown_files(docs) if p.is_file())
    for path in sorted(candidates):
        rel = path.relative_to(root).as_posix()
        if _is_test_count_excluded(rel):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="strict")
        except (OSError, UnicodeError):
            continue
        for rx in TEST_COUNT_RES:
            for match in rx.finditer(text):
                line = text.count("\n", 0, match.start()) + 1
                key = (rel, line)
                if key in seen_lines:
                    continue
                seen_lines.add(key)
                claims.append(f"{rel}:{line}: {match.group(0).strip()}")
    return sorted(claims)


def check(root: pathlib.Path) -> list[str]:
    errors = []
    for rel in find_nested_readmes(root):
        errors.append(f"nested README: {rel}")
    for item in find_broken_local_links(root):
        errors.append(f"broken link: {item}")
    for item in find_test_count_claims(root):
        errors.append(f"test-count claim: {item}")
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fail-closed docs contract checks (issue #9).")
    parser.add_argument("--root", default=str(ROOT), help="Repository root to scan.")
    args = parser.parse_args(argv)
    errors = check(pathlib.Path(args.root).resolve())
    if errors:
        for err in errors:
            print(err)
        print(f"docs contract FAILED: {len(errors)} problem(s)")
        return 1
    print("docs contract OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
