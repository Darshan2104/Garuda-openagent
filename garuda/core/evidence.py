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

_SEGMENT_OPERATORS = ("&&", "||", "|", ";", "\n")


def split_segments(command: str) -> list[str]:
    """Split on shell control operators, ignoring any that appear inside quotes.

    A quote-blind split is not merely imprecise, it is exploitable in the
    direction that matters: `echo "run x && grep -q y"` splits into a fragment
    containing `grep -q y`, which grades as an assertion, so a command that only
    prints a string passes the gate. It also silently disabled the `python -c`
    guard below, because the `;` separating statements inside the quoted body
    tore the one-liner into fragments that no longer looked like `python -c`.
    """
    segments: list[str] = []
    current: list[str] = []
    quote: str | None = None
    index = 0
    text = command or ""
    while index < len(text):
        char = text[index]
        if quote is not None:
            current.append(char)
            # Only double quotes honour backslash escapes in the shell.
            if char == "\\" and quote == '"' and index + 1 < len(text):
                current.append(text[index + 1])
                index += 2
                continue
            if char == quote:
                quote = None
            index += 1
            continue
        if char in "\"'":
            quote = char
            current.append(char)
            index += 1
            continue
        if char == "\\" and index + 1 < len(text):
            current.append(char)
            current.append(text[index + 1])
            index += 2
            continue
        operator = next((op for op in _SEGMENT_OPERATORS if text.startswith(op, index)), None)
        if operator is not None:
            segments.append("".join(current))
            current = []
            index += len(operator)
            continue
        current.append(char)
        index += 1
    segments.append("".join(current))
    return [segment for segment in segments if segment.strip()]


_PYTHON_ONELINER = re.compile(r"""^python3?\s+-c\s+["'](?P<body>.*)["']$""", re.DOTALL)
_PY_IMPORT = re.compile(r"^(import|from)\s+\w")
_PY_PRINT = re.compile(r"^print\s*\(")
_PY_ASSERTION = re.compile(r"^(assert|raise)\b")
_PY_EXIT = re.compile(r"\b(?:sys\s*\.\s*exit|os\s*\.\s*_exit)\s*\(")
# A name (possibly attributed, subscripted, or a tuple target) followed by a
# single `=`. `==` is a comparison, so the lookahead matters. `(` is excluded
# deliberately: an assignment target never contains one, so `solve.run(strict=1)`
# stays a call that runs the deliverable rather than being read as an assignment.
_PY_ASSIGN = re.compile(r"^[A-Za-z_][\w\.\[\]'\"\s,]*=(?!=)")


def classify_python_oneliner(segment: str) -> str | None:
    """Class of a `python -c "..."` one-liner, or None if the segment isn't one.

    An exit code only carries information about correctness when something in
    the body can raise *because a value is wrong*. Imports, assignments and
    prints cannot: `m=pickle.load(open(p)); print(m.shape)` exits 0 for a model
    that deserialized fine and was trained wrong — exactly the claim `cat`
    makes, dressed as computation. A bare call such as `solve.main()` does run
    the deliverable, so it keeps the weaker-but-real EXECUTION class.
    """
    match = _PYTHON_ONELINER.match(segment.strip())
    if not match:
        return None
    statements = [s.strip() for s in re.split(r"[;\n]", match.group("body")) if s.strip()]
    if not statements:
        return SYNTAX
    if any(_PY_ASSERTION.match(s) or _PY_EXIT.search(s) for s in statements):
        return ASSERTION
    for statement in statements:
        if _PY_IMPORT.match(statement) or _PY_PRINT.match(statement):
            continue
        if _PY_ASSIGN.match(statement):
            continue
        return EXECUTION
    return SYNTAX


def classify_segment(segment: str) -> str:
    """Classify one pipeline segment by the strongest claim its exit code supports."""
    segment = segment.strip()
    if not segment:
        return NOOP
    for pattern in SYNTAX_ONLY_PATTERNS:
        if pattern.search(segment):
            return SYNTAX
    oneliner = classify_python_oneliner(segment)
    if oneliner is not None:
        return oneliner
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
    for segment in split_segments(command):
        label = classify_segment(segment)
        if _STRENGTH[label] > _STRENGTH[best]:
            best = label
    return best


def is_discriminating(command: str) -> bool:
    """True when a wrong result could make this command exit non-zero."""
    return _STRENGTH[classify_command(command)] >= DISCRIMINATING_THRESHOLD


# Programs that only read. Note this is a *different axis* from the classification
# above: that one ranks how much a command's exit code proves, this one asks whether
# running it changes anything. They are close to opposites in practice — the commands
# that prove the most (`pytest`, `make`, the deliverable itself) are exactly the ones
# that write — which is why concurrency here is a narrow win and not a broad one.
SIDE_EFFECT_FREE_COMMANDS = INSPECTION_COMMANDS | frozenset(
    {"diff", "cmp", "test", "[", "[[", "grep", "rg", "egrep", "fgrep", "jq", "sort", "uniq", "cut"}
)

# Shell constructs that write, or hand off to something that might, regardless of
# which program is being run. Checked against the raw command because they are
# syntax, not argv[0].
_MUTATING_SHELL = re.compile(r">|\btee\b|\bsudo\b|\bxargs\b|`|\$\(")

# A trailing `&` backgrounds the segment so it outlives the check; `&&` does not.
_TRAILING_BACKGROUND = re.compile(r".*(?<!&)&\s*$", re.DOTALL)


def is_side_effect_free(command: str) -> bool:
    """True only when running this command cannot change anything observable.

    Used to decide which verification commands may run concurrently. An allowlist
    that fails closed: anything unrecognised — every interpreter, every build tool,
    every script in the workspace — is assumed to write, because the cost of being
    wrong is a race between two commands at the completion gate, and the cost of
    being conservative is only that they run one after another as before.

    Reuses ``split_segments`` so quoting is handled the same way the discriminating
    classifier handles it: ``echo "cat a > b"`` prints a string and writes nothing,
    while ``cat a > b`` writes, and a quote-blind check cannot tell them apart.
    """
    text = command or ""
    if not text.strip():
        return False
    segments = split_segments(text)
    if not segments:
        return False
    for segment in segments:
        # `&` backgrounds the segment, so it outlives the check and can do anything
        # afterwards. `>` and friends are looked for per segment for the same reason
        # split_segments exists: the operators only mean redirection outside quotes.
        if _MUTATING_SHELL.search(segment) or _TRAILING_BACKGROUND.match(segment):
            return False
        head = _command_head(segment)
        # A segment that is only `cd x` or env assignments resolves to no program.
        # `cd` cannot be treated as harmless here: it moves the working directory for
        # everything after it in the same command, so its effect is exactly the kind
        # of ordering dependence that makes concurrency unsafe.
        if head is None or head not in SIDE_EFFECT_FREE_COMMANDS:
            return False
    return True


def _command_head(segment: str) -> str | None:
    """Resolved program name for one segment, or None if it runs no program.

    Skips leading env assignments and wrapper prefixes, so ``FOO=1 timeout 5 cat x``
    is judged on ``cat``. Unlike ``classify_segment`` it does **not** skip ``sudo``
    (caught as mutating by the caller) or ``cd`` (which resolves to ``cd``, is absent
    from the allowlist, and therefore makes the whole command ineligible).
    """
    try:
        tokens = shlex.split(segment)
    except ValueError:
        tokens = segment.split()
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if "=" in token and not token.startswith("-") and re.match(r"^\w+=", token):
            index += 1
            continue
        if token in ("env", "nohup", "time", "timeout", "stdbuf", "nice"):
            index += 1
            while index < len(tokens) and re.match(r"^-|^\d+[smhd]?$", tokens[index]):
                index += 1
            continue
        break
    if index >= len(tokens):
        return None
    return tokens[index].rsplit("/", 1)[-1]


def weakness_reason(command: str) -> str:
    """Explain, for the model, why a command does not count as evidence."""
    label = classify_command(command)
    if label == SYNTAX and any(
        classify_python_oneliner(segment) == SYNTAX for segment in split_segments(command)
    ):
        return (
            "loads the artifact and prints what it found — nothing in it raises when a "
            "value is wrong, so it exits 0 for output that deserializes perfectly and is "
            "incorrect. Add an assert comparing what you got against what the task "
            "requires, so a wrong value makes it exit non-zero"
        )
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
