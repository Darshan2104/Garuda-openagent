"""Does a verification command actually discriminate between right and wrong?

An agent that chooses its own oracle will choose one that cannot fail. `cat
output.txt`, `ls -la`, `echo "Expected: 42"` and `python -m py_compile x.py` all
exit 0 whether or not the task was solved: they prove an artifact exists or
parses, never that it is correct. Treating their exit code as evidence turns the
completion gate into a formality.

This module classifies a shell command by the strongest claim its exit status
can support, so the gate can insist on at least one command whose exit code
would change if the work were wrong.

Classification is structural, not semantic — it reasons about which programs
propagate failure, and knows nothing about any particular task or benchmark.
"""

import re
import shlex

# Reports existence or content but exits 0 regardless of correctness.
INSPECTION_COMMANDS = frozenset(
    {
        "cat", "ls", "echo", "printf", "head", "tail", "wc", "od", "xxd", "hexdump",
        "stat", "file", "which", "type", "pwd", "dirname", "basename", "realpath",
        "readlink", "true", "date", "whoami", "id", "env", "printenv", "du", "df",
        "tree", "less", "more", "nl", "strings", "sha256sum", "md5sum", "cksum",
    }
)

# Proves the artifact parses; says nothing about behaviour.
SYNTAX_ONLY_PATTERNS = (
    re.compile(r"\bpy_compile\b"),
    re.compile(r"\bpython3?\s+-m\s+compileall\b"),
    re.compile(r"\bbash\s+-n\b"),
    re.compile(r"\bsh\s+-n\b"),
    re.compile(r"\bnode\s+--check\b"),
    re.compile(r"\btsc\s+--noEmit\b"),
    re.compile(r"-fsyntax-only\b"),
    re.compile(r"\bruff\s+check\b"),
    re.compile(r"\bjson\.tool\b"),
)

# Programs whose non-zero exit means "the thing under test is wrong".
ASSERTION_COMMANDS = frozenset(
    {
        "test", "[", "[[", "diff", "cmp", "pytest", "unittest", "assert",
        "grep", "rg", "egrep", "fgrep", "jq", "curl", "nc", "ping", "make",
        "cargo", "go", "npm", "yarn", "pnpm", "ctest", "tox", "nose2", "mvn",
        "gradle", "phpunit", "rspec", "busted", "shellcheck",
    }
)

# `grep`/`jq`/`curl` only propagate failure with the right flags; without them
# they are inspection. These flags are what turn them into assertions.
_ASSERTING_FLAGS = {
    "grep": ("-q", "--quiet", "-c", "--count"),
    "rg": ("-q", "--quiet", "-c", "--count"),
    "egrep": ("-q", "--quiet"),
    "fgrep": ("-q", "--quiet"),
    "curl": ("-f", "--fail", "--fail-with-body"),
    "jq": ("-e", "--exit-status"),
}

# Class labels, weakest to strongest.
NOOP = "noop"
INSPECTION = "inspection"
SYNTAX = "syntax"
EXECUTION = "execution"
ASSERTION = "assertion"

_STRENGTH = {NOOP: 0, INSPECTION: 1, SYNTAX: 2, EXECUTION: 3, ASSERTION: 4}

# Below this, a command cannot distinguish a correct result from a wrong one.
DISCRIMINATING_THRESHOLD = _STRENGTH[EXECUTION]

_SEGMENT_SPLIT = re.compile(r"\|\||&&|\||;|\n")

_BARE_IMPORT = re.compile(
    r"""^python3?\s+-c\s+["'](?P<body>.*)["']$""", re.DOTALL
)


def _is_bare_import(segment: str) -> bool:
    """True for `python -c "import x"` — proves importability, not behaviour."""
    match = _BARE_IMPORT.match(segment.strip())
    if not match:
        return False
    body = match.group("body")
    statements = [s.strip() for s in re.split(r"[;\n]", body) if s.strip()]
    if not statements:
        return True
    for statement in statements:
        if re.match(r"^(import|from)\s+\w", statement):
            continue
        if re.match(r"^print\s*\(", statement):
            continue
        return False
    return True


def classify_segment(segment: str) -> str:
    """Classify one pipeline segment by the strongest claim its exit code supports."""
    segment = segment.strip()
    if not segment:
        return NOOP
    for pattern in SYNTAX_ONLY_PATTERNS:
        if pattern.search(segment):
            return SYNTAX
    if _is_bare_import(segment):
        return SYNTAX
    try:
        tokens = shlex.split(segment)
    except ValueError:
        tokens = segment.split()
    if not tokens:
        return NOOP
    # Skip leading env assignments and common prefixes so `cd /app && X` and
    # `FOO=1 timeout 5 X` are judged on X.
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if "=" in token and not token.startswith("-") and re.match(r"^\w+=", token):
            index += 1
            continue
        if token in ("sudo", "env", "nohup", "time", "timeout", "stdbuf", "nice"):
            index += 1
            # `timeout 30 cmd` — drop its numeric argument too.
            while index < len(tokens) and re.match(r"^-|^\d+[smhd]?$", tokens[index]):
                index += 1
            continue
        break
    if index >= len(tokens):
        return NOOP
    head = tokens[index].rsplit("/", 1)[-1]
    rest = tokens[index + 1 :]

    if head == "cd":
        return NOOP
    if head in _ASSERTING_FLAGS:
        flags = _ASSERTING_FLAGS[head]
        return ASSERTION if any(flag in rest for flag in flags) else INSPECTION
    if head in ASSERTION_COMMANDS:
        return ASSERTION
    if head in INSPECTION_COMMANDS:
        return INSPECTION
    # Anything else runs a program: a script, binary, or interpreter invocation.
    # Its exit code reflects whether that program succeeded, which is real —
    # weaker than an explicit assertion, strong enough to count as evidence.
    return EXECUTION


def classify_command(command: str) -> str:
    """Strongest classification across a command's segments.

    A pipeline is as strong as its strongest link because shells propagate the
    failure: `cat out.txt | grep -q EXPECTED` fails when the content is wrong,
    even though `cat` alone never would.
    """
    best = NOOP
    for segment in _SEGMENT_SPLIT.split(command or ""):
        label = classify_segment(segment)
        if _STRENGTH[label] > _STRENGTH[best]:
            best = label
    return best


def is_discriminating(command: str) -> bool:
    """True when a wrong result could make this command exit non-zero."""
    return _STRENGTH[classify_command(command)] >= DISCRIMINATING_THRESHOLD


def weakness_reason(command: str) -> str:
    """Explain, for the model, why a command does not count as evidence."""
    label = classify_command(command)
    if label == SYNTAX:
        return (
            "only checks that the file parses or imports — it exits 0 for code that "
            "parses perfectly and computes the wrong answer"
        )
    if label == INSPECTION:
        return (
            "only displays or lists content — it exits 0 whether the content is right "
            "or wrong, so its exit code proves nothing"
        )
    return "cannot fail, so it carries no information about correctness"


def summarize(commands: list[str]) -> dict[str, object]:
    """Classification summary for a set of verification commands."""
    labels = {command: classify_command(command) for command in commands}
    discriminating = [c for c, label in labels.items() if _STRENGTH[label] >= DISCRIMINATING_THRESHOLD]
    return {
        "labels": labels,
        "n_commands": len(commands),
        "n_discriminating": len(discriminating),
        "discriminating": discriminating,
        "weak": [c for c in commands if c not in discriminating],
    }
