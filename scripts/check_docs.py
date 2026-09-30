"""Documentation contract checks for issue #9 (P0.3).

Four dependency-free checks, local only — no remote fetching:

1. Broken local Markdown links: every relative ``[text](target)`` in a tracked
   ``*.md`` file must resolve to an existing path. Remote URLs, mailto links,
   and pure anchors are skipped, never fetched.
2. Nested READMEs: only the root ``README.md`` may exist. Anything else named
   ``README.md`` (case-insensitive) outside the excluded directories fails.
3. Hand-maintained test counts: release docs must not claim a specific test
   count (e.g. "1131 tests passing"). Historical records under ``docs/archive/``
   are excluded; everything else under ``README.md``, ``AGENTS.md`` and
   ``docs/`` is checked.
4. Duplicate Markdown table rows in maintained user documentation.

With ``--commands``, a fifth check imports Garuda's argparse parser and verifies
every ``garuda ...`` command in maintained fenced and inline code. This mode
requires the package to be installed; the default remains stdlib-only.

Exceptions must be explicit: add the path to ``ALLOWED_NESTED_READMES`` below
and get it reviewed — there is currently no exception.

Usage:
    python scripts/check_docs.py [--root PATH] [--commands]
"""

from __future__ import annotations

import argparse
import pathlib
import re
import shlex
import sys
from typing import NamedTuple

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

USER_DOC_EXCLUDED_FILES = frozenset(
    {
        "docs/ARCHITECTURE.md",
        "docs/BACKLOG.md",
        "docs/MODULES.md",
        "docs/major-changes.md",
    }
)
USER_DOC_EXCLUDED_PREFIXES = (
    "docs/archive/",
    "docs/design/",
    "docs/plans/",
    "docs/roadmap/",
)
COMMAND_FENCE_LANGUAGES = frozenset({"bash", "console", "sh", "shell", "text", "zsh"})
COMMAND_IGNORE_MARKER = "docs-command-ignore"

LINK_RE = re.compile(r"\[[^\]]*\]\(([^)]+)\)")
SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*:")
TEST_COUNT_RES = (
    re.compile(r"\b\d{3,}\s+tests?\b", re.IGNORECASE),
    re.compile(r"\b\d+\s+tests?\s+(passing|green|failing|covered|covering)\b", re.IGNORECASE),
)
FENCE_OPEN_RE = re.compile(r"^\s*(?P<fence>`{3,}|~{3,})(?P<info>.*)$")
INLINE_CODE_RE = re.compile(
    r"(?<!`)(?P<ticks>`+)(?!`)(?P<code>.*?)(?P=ticks)(?!`)", re.DOTALL
)
OPTIONAL_GROUP_RE = re.compile(r"\[([^\[\]]+)\]")
TABLE_SEPARATOR_CELL_RE = re.compile(r"^:?-{3,}:?$")


class DocumentedCommand(NamedTuple):
    path: str
    line: int
    command: str


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


def iter_maintained_markdown_files(root: pathlib.Path) -> list[pathlib.Path]:
    """Return current user-facing docs, excluding history, plans, and specs."""
    files: set[pathlib.Path] = set()
    readme = root / "README.md"
    if readme.is_file():
        files.add(readme)
    docs = root / "docs"
    if docs.is_dir():
        for path in docs.rglob("*.md"):
            if _is_excluded(path, root):
                continue
            rel = path.relative_to(root).as_posix()
            if rel in USER_DOC_EXCLUDED_FILES:
                continue
            if rel.startswith(USER_DOC_EXCLUDED_PREFIXES):
                continue
            files.add(path)
    return sorted(files)


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


def _table_cells(line: str) -> tuple[str, ...] | None:
    stripped = line.strip()
    if not (stripped.startswith("|") and stripped.endswith("|")):
        return None
    cells = tuple(re.sub(r"\s+", " ", cell.strip()) for cell in stripped[1:-1].split("|"))
    return cells if len(cells) >= 2 else None


def _is_table_separator(cells: tuple[str, ...]) -> bool:
    return all(TABLE_SEPARATOR_CELL_RE.fullmatch(cell.replace(" ", "")) for cell in cells)


def find_duplicate_table_rows(root: pathlib.Path) -> list[str]:
    duplicates = []
    for path in iter_maintained_markdown_files(root):
        try:
            lines = path.read_text(encoding="utf-8", errors="strict").splitlines()
        except (OSError, UnicodeError):
            continue
        rel = path.relative_to(root).as_posix()
        seen: dict[tuple[str, ...], int] = {}
        for line_number, line in enumerate(lines, start=1):
            cells = _table_cells(line)
            if cells is None:
                seen = {}
                continue
            if _is_table_separator(cells):
                continue
            first_line = seen.get(cells)
            if first_line is not None:
                rendered = " | ".join(cells)
                duplicates.append(
                    f"{rel}:{line_number}: duplicate table row "
                    f"(first at line {first_line}): {rendered}"
                )
            else:
                seen[cells] = line_number
    return sorted(duplicates)


def _normalize_shell_line(line: str) -> str:
    stripped = line.strip()
    if stripped.startswith("$ "):
        stripped = stripped[2:].lstrip()
    return stripped


def _commands_from_shell_block(
    lines: list[tuple[int, str]], rel: str
) -> list[DocumentedCommand]:
    commands = []
    pending = ""
    pending_line = 0
    ignored = False
    for line_number, raw in lines:
        line = _normalize_shell_line(raw)
        if not pending and (not line or line.startswith("#")):
            continue
        if not pending:
            pending_line = line_number
            ignored = COMMAND_IGNORE_MARKER in line
        if line.endswith("\\"):
            pending += line[:-1].rstrip() + " "
            continue
        pending += line
        normalized = re.sub(r"\s+", " ", pending).strip()
        if not ignored and re.match(r"^garuda(?:\s|$)", normalized):
            commands.append(DocumentedCommand(rel, pending_line, normalized))
        pending = ""
        pending_line = 0
        ignored = False
    return commands


def _extract_commands(path: pathlib.Path, root: pathlib.Path) -> list[DocumentedCommand]:
    try:
        text = path.read_text(encoding="utf-8", errors="strict")
    except (OSError, UnicodeError):
        return []
    rel = path.relative_to(root).as_posix()
    lines = text.splitlines(keepends=True)
    outside = list(lines)
    commands: list[DocumentedCommand] = []
    in_fence = False
    fence_enabled = False
    fence_character = ""
    fence_length = 0
    block: list[tuple[int, str]] = []
    for index, raw in enumerate(lines):
        line_number = index + 1
        stripped_line = raw.rstrip("\r\n")
        opening = FENCE_OPEN_RE.match(stripped_line) if not in_fence else None
        closing = bool(
            in_fence
            and re.fullmatch(
                rf"\s*{re.escape(fence_character)}{{{fence_length},}}\s*",
                stripped_line,
            )
        )
        if opening or closing:
            outside[index] = "\n" if raw.endswith("\n") else ""
            if not in_fence:
                fence = opening.group("fence")
                info = opening.group("info").strip()
                language = info.split()[0].lower() if info else ""
                fence_enabled = (
                    language in COMMAND_FENCE_LANGUAGES
                    and COMMAND_IGNORE_MARKER not in info
                )
                fence_character = fence[0]
                fence_length = len(fence)
                block = []
                in_fence = True
            else:
                if fence_enabled:
                    commands.extend(_commands_from_shell_block(block, rel))
                in_fence = False
                fence_enabled = False
                fence_character = ""
                fence_length = 0
                block = []
            continue
        if in_fence:
            outside[index] = "\n" if raw.endswith("\n") else ""
            block.append((line_number, raw.rstrip("\r\n")))
    if in_fence and fence_enabled:
        commands.extend(_commands_from_shell_block(block, rel))

    outside_text = "".join(outside)
    for match in INLINE_CODE_RE.finditer(outside_text):
        command = re.sub(r"\s+", " ", match.group("code")).strip()
        if not re.match(r"^garuda(?:\s|$)", command):
            continue
        line_number = outside_text.count("\n", 0, match.start()) + 1
        line_end = outside_text.find("\n", match.end())
        if line_end == -1:
            line_end = len(outside_text)
        if COMMAND_IGNORE_MARKER in outside_text[match.end() : line_end]:
            continue
        commands.append(DocumentedCommand(rel, line_number, command))
    return commands


def find_documented_commands(root: pathlib.Path) -> list[DocumentedCommand]:
    commands = []
    for path in iter_maintained_markdown_files(root):
        commands.extend(_extract_commands(path, root))
    return sorted(commands)


def _normalize_reference_syntax(command: str) -> str:
    previous = None
    while previous != command:
        previous = command
        command = OPTIONAL_GROUP_RE.sub(r"\1", command)
    return command


def _subparsers_action(parser: argparse.ArgumentParser):
    return next(
        (action for action in parser._actions if isinstance(action, argparse._SubParsersAction)),
        None,
    )


def _is_placeholder(value: str) -> bool:
    return (value.startswith("<") and value.endswith(">")) or (
        value.isalpha() and value.upper() == value
    )


def _option_value_count(action: argparse.Action, remaining: list[str]) -> int:
    if action.nargs == 0:
        return 0
    if action.nargs in (None, 1):
        return 1
    if action.nargs == "?":
        return 0 if not remaining or remaining[0].startswith("-") else 1
    if isinstance(action.nargs, int):
        return action.nargs
    if action.nargs in ("*", "+"):
        count = 0
        for token in remaining:
            if token.startswith("-") or token in {"...", "…"}:
                break
            count += 1
        return count
    return 1


def validate_documented_command(
    command: str, parser: argparse.ArgumentParser
) -> list[str]:
    normalized = _normalize_reference_syntax(command)
    try:
        tokens = shlex.split(normalized)
    except ValueError as exc:
        return [f"cannot parse shell syntax: {exc}"]
    if not tokens or tokens[0] != "garuda":
        return ["command must start with garuda"]

    current = parser
    index = 1
    while index < len(tokens):
        token = tokens[index]
        if token in {"...", "…"}:
            break
        subparsers = _subparsers_action(current)
        if subparsers is not None and token in subparsers.choices:
            current = subparsers.choices[token]
            index += 1
            continue
        if token.startswith("-"):
            option, has_equals, inline_value = token.partition("=")
            action = current._option_string_actions.get(option)
            if action is None:
                return [f"unknown option {option!r}"]
            if has_equals and action.nargs == 0:
                return [f"option {option!r} does not accept a value"]
            if has_equals:
                values = [inline_value]
                consumed = 0
            else:
                remaining = tokens[index + 1 :]
                consumed = _option_value_count(action, remaining)
                if consumed > len(remaining):
                    return [f"option {option!r} is missing a value"]
                values = remaining[:consumed]
            if action.choices is not None:
                for value in values:
                    if not _is_placeholder(value) and value not in action.choices:
                        return [f"invalid choice {value!r} for {option!r}"]
            index += consumed + 1
            continue
        if subparsers is not None:
            return [f"unknown subcommand {token!r}"]
        index += 1
    return []


def find_invalid_documented_commands(
    root: pathlib.Path, parser: argparse.ArgumentParser
) -> list[str]:
    errors = []
    for item in find_documented_commands(root):
        for error in validate_documented_command(item.command, parser):
            errors.append(f"{item.path}:{item.line}: {error}: {item.command}")
    return sorted(errors)


def check(root: pathlib.Path) -> list[str]:
    errors = []
    for rel in find_nested_readmes(root):
        errors.append(f"nested README: {rel}")
    for item in find_broken_local_links(root):
        errors.append(f"broken link: {item}")
    for item in find_test_count_claims(root):
        errors.append(f"test-count claim: {item}")
    for item in find_duplicate_table_rows(root):
        errors.append(f"duplicate table row: {item}")
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fail-closed docs contract checks (issue #9).")
    parser.add_argument("--root", default=str(ROOT), help="Repository root to scan.")
    parser.add_argument(
        "--commands",
        action="store_true",
        help="Also validate documented commands against the installed Garuda parser.",
    )
    args = parser.parse_args(argv)
    root = pathlib.Path(args.root).resolve()
    errors = check(root)
    if args.commands:
        try:
            from garuda.interfaces.main import build_parser
        except Exception as exc:
            print(
                f"docs command check unavailable: cannot import Garuda parser: {exc}",
                file=sys.stderr,
            )
            return 2
        for item in find_invalid_documented_commands(root, build_parser()):
            errors.append(f"invalid command: {item}")
    if errors:
        for err in errors:
            print(err)
        print(f"docs contract FAILED: {len(errors)} problem(s)")
        return 1
    print("docs contract OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
