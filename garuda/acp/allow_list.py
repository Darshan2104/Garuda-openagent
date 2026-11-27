"""A role's approval allow-list for unattended flow steps (#861).

In a flow nobody answers a harness's ``session/request_permission``: a
foreground step's asks are denied, a background step's park and then time out.
A role may name shell commands its harness may run without a person:

```yaml
roles:
  coder:
    harness: claude
    approvals:
      allow: ["python -m pytest*", "npm test*"]
```

An ask is auto-approved only when **all** of these hold (anything else takes
the usual path, which in an unattended flow is a denial):

- the request's ``toolCall.kind`` is ``execute`` and it offers an
  ``allow_once`` option;
- its ``rawInput.command`` is readable: a string, a ``[shell, -c|-lc, script]``
  list (the script), or a list of plain words; any ``rawInput.cwd`` is inside
  the step's workspace;
- the command holds no shell control or expansion character
  (:data:`FORBIDDEN`) and no other control character, and is at most
  :data:`MAX_COMMAND_CHARS` long;
- the whole command, trimmed and with whitespace collapsed, matches one
  pattern with shell-glob semantics (:func:`fnmatch.fnmatchcase`).

Only flow steps that may edit use it: a no-edits step and a read-only
(confined) role never do, nor do ``garuda run`` and the SDK.
"""

from __future__ import annotations

import fnmatch
import os
from dataclasses import dataclass
from pathlib import Path

#: At most this many patterns per role, each at most this long.
MAX_PATTERNS = 32
MAX_PATTERN_CHARS = 200
#: A longer command is never auto-approved.
MAX_COMMAND_CHARS = 2000
#: Shell control and expansion characters. A command holding any of them is never
#: auto-approved, whatever it matches: ``python -m pytest; rm -rf ~`` must not pass.
FORBIDDEN = (";", "&", "|", "`", "$", ">", "<", "\\", "\n", "\r")
#: Glob characters a pattern's first word (the program) may not hold.
GLOB = ("*", "?", "[")
#: Shells whose ``[shell, -c, script]`` argv is read as ``script``.
SHELLS = frozenset({"sh", "bash", "zsh", "dash"})


def normalize(text: str) -> str:
    """Trimmed, with every run of whitespace collapsed to one space."""
    return " ".join(text.split())


def forbidden_in(text: str) -> str | None:
    """The first shell control/expansion or control character in ``text``, or ``None``."""
    for char in text:
        if char in FORBIDDEN or (ord(char) < 32 and char != "\t") or ord(char) == 127:
            return char
    return None


def pattern_problem(pattern: str) -> str | None:
    """Why ``pattern`` is not a usable allow-list pattern, or ``None``."""
    if len(pattern) > MAX_PATTERN_CHARS:
        return f"is longer than {MAX_PATTERN_CHARS} characters"
    char = forbidden_in(pattern)
    if char is not None:
        return (f"holds {char!r}; a command with shell control or expansion characters is "
                "never auto-approved, so this pattern could never match")
    program = normalize(pattern).split(" ")[0]
    if any(glob in program for glob in GLOB):
        return "must start with a literal program name (no '*', '?' or '[' in its first word)"
    return None


@dataclass(frozen=True)
class AllowListMatch:
    """An auto-approved ask: the pattern that matched and the command it matched."""

    pattern: str
    command: str


def command_of(tool_call: dict) -> str | None:
    """The shell command an ``execute`` ask would run, or ``None`` when unreadable."""
    raw = tool_call.get("rawInput")
    command = raw.get("command") if isinstance(raw, dict) else None
    if isinstance(command, str):
        return command
    if (not isinstance(command, list) or not command
            or not all(isinstance(part, str) and part for part in command)):
        return None
    if len(command) == 3 and os.path.basename(command[0]) in SHELLS and command[1] in ("-c", "-lc"):
        return command[2]
    # Joining argv words loses their quoting: only plain words are read.
    if any(char.isspace() or char in "'\"" for part in command for char in part):
        return None
    return " ".join(command)


def _inside(cwd, workspace) -> bool:
    if not isinstance(cwd, str) or not cwd or workspace is None:
        return False
    try:
        root = Path(workspace).resolve(strict=True)
        path = (root / cwd).resolve()
    except (OSError, RuntimeError):
        return False
    return path == root or path.is_relative_to(root)


def match(tool_call: dict, patterns, workspace) -> AllowListMatch | None:
    """The allow-list match for one permission request's ``toolCall``, or ``None``.

    Fail-closed: anything unexpected is ``None``, and the ask takes the usual path.
    """
    if not patterns or not isinstance(tool_call, dict) or tool_call.get("kind") != "execute":
        return None
    raw = tool_call.get("rawInput")
    if isinstance(raw, dict) and "cwd" in raw and not _inside(raw["cwd"], workspace):
        return None
    command = command_of(tool_call)
    if (command is None or len(command) > MAX_COMMAND_CHARS
            or forbidden_in(command) is not None):
        return None
    text = normalize(command)
    if not text:
        return None
    for pattern in patterns:
        if (isinstance(pattern, str) and pattern_problem(pattern) is None
                and fnmatch.fnmatchcase(text, normalize(pattern))):
            return AllowListMatch(pattern=pattern, command=text)
    return None
