import fnmatch
import re
import shlex
from enum import Enum
from pathlib import Path
from typing import Awaitable, Callable

ApprovalHandler = Callable[[str], Awaitable[bool]]


class PermissionDecision(str, Enum):
    ALLOW = "allow"
    DENY = "deny"
    ASK = "ask"


_DECISION_PRECEDENCE = {
    PermissionDecision.ALLOW: 0,
    PermissionDecision.ASK: 1,
    PermissionDecision.DENY: 2,
}


def _strictest(*decisions: PermissionDecision) -> PermissionDecision:
    """Return the strictest decision (DENY > ASK > ALLOW)."""
    return max(decisions, key=lambda d: _DECISION_PRECEDENCE[d])


# Shell metacharacters that chain/redirect/substitute a second command. A command
# containing any of these must not ride behind an allow-prefix fast-path.
_SHELL_CHAIN_RE = re.compile(r"[;|&\n<>]|\$\(|`")


# Flag clusters that mean "recursive" and "force" to `rm`, in either order, as
# short bundles (-rf/-fr/-Rf), separate flags, or long options. The previous
# `rm\s+-rf\s+/` matched one spelling of many: `rm -fr /` and
# `rm --recursive --force /` both walked straight through.
_RM_RECURSIVE = r"(?:-[a-z]*r[a-z]*\b|--recursive\b|-[a-z]*R[a-z]*\b)"
_RM_FORCE = r"(?:-[a-z]*f[a-z]*\b|--force\b)"
# Root, or a variable/glob that expands to it (`rm -rf "$DIR"/` with DIR unset).
_ROOT_TARGET = r"(?:/|/\*|\$\{?\w+\}?/?|~/?)\s*$"

DENY_COMMAND_PATTERNS = [
    # rm, with recursive+force in any order/spelling, targeting root. `git rm` is
    # excluded: it stages a deletion in the index, it does not unlink a tree.
    re.compile(
        rf"(?<!git )\brm\s+(?:{_RM_RECURSIVE}|{_RM_FORCE}|\s)*"
        rf"(?:{_RM_RECURSIVE}\s+{_RM_FORCE}|{_RM_FORCE}\s+{_RM_RECURSIVE}|-[a-z]*[rR][a-z]*f|"
        rf"-[a-z]*f[a-z]*[rR])[a-z]*\s+{_ROOT_TARGET}",
        re.IGNORECASE,
    ),
    re.compile(r"mkfs\.", re.IGNORECASE),
    # `dd` writing to a block device, in either operand order — `dd of=… if=…` is
    # the same command as `dd if=… of=…`. Only a /dev/ *target* is denied: an
    # ordinary `dd if=in.img of=out.img` is a file copy, and blanket-denying every
    # `dd if=` (as this list used to) blocked legitimate work.
    re.compile(r"\bdd\b(?=[^|;&]*\bof=\s*/dev/)", re.IGNORECASE),
    re.compile(r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;", re.IGNORECASE),
]

ASK_COMMAND_PATTERNS = [
    # Any recursive rm, not just the `-rf` spelling.
    re.compile(rf"(?<!git )\brm\s+(?:[a-z-]*\s+)*{_RM_RECURSIVE}", re.IGNORECASE),
    # Reading a raw device is exfiltration-shaped rather than destructive: ask.
    re.compile(r"\bdd\b(?=[^|;&]*\bif=\s*/dev/)", re.IGNORECASE),
    re.compile(r"\bsudo\b", re.IGNORECASE),
    re.compile(r"\bchmod\b", re.IGNORECASE),
    re.compile(r"\bchown\b", re.IGNORECASE),
    # Piping a download into a shell, via curl or wget, to sh/bash/zsh/python.
    re.compile(r"\b(?:curl|wget)\b[^|;&]*\|\s*(?:sudo\s+)?(?:ba|z|d)?sh\b", re.IGNORECASE),
    re.compile(r"\b(?:curl|wget)\b[^|;&]*\|\s*(?:sudo\s+)?python[23]?\b", re.IGNORECASE),
]

# Tools whose primary argument is a shell command that must be screened.
COMMAND_TOOLS = {
    "bash": "command",
    "bash_background": "command",
    "tmux_exec": "command",
}

# Search tools and the path-ish arguments to screen on each. These reach the
# filesystem through ``env.execute`` rather than the file tools, so without this
# they bypass ``path_rules`` entirely (``ls secrets/`` evading a deny rule that
# blocks ``read_file`` on the same directory).
SEARCH_TOOLS = {
    "grep": ("path", "glob"),
    "glob": ("path", "pattern"),
    "ls": ("path",),
}

# File-operation tools that modify the filesystem.
WRITE_TOOLS = {"write_file", "edit", "multi_edit"}

# File-operation tools that only read.
READ_TOOLS = {"read_file", "read_pdf", "read_spreadsheet"}

READONLY_DENIED_TOOLS = {"write_file", "edit", "multi_edit", "tmux_exec", "bash_background", "kill_task"}


def command_path_tokens(command: str) -> list[str]:
    """Best-effort extraction of path-like operands from a shell command.

    Used to screen ``bash``/``tmux_exec`` arguments against ``path_rules`` so
    ``cat .env`` is caught by the same deny rule as ``read_file(".env")``. Flags
    are skipped; everything else (including argv[0]) is screened, which can only
    err toward *stricter* on a deny rule. Unbalanced quotes fall back to a
    whitespace split rather than failing open.
    """
    try:
        tokens = shlex.split(command, comments=True)
    except ValueError:
        tokens = command.split()
    return [t for t in tokens if t and not t.startswith("-")]


class PermissionEngine:
    """Policy engine screening tool calls, file paths, and shell commands.

    ``bash_rules`` supports three keys:

    - ``deny``: list of regexes; a match denies the command. Deny always wins.
    - ``allow_prefixes``: list of literal command prefixes (e.g. ``"git status"``,
      ``"npm test"``). After stripping leading whitespace, a command that equals
      an allowed prefix or starts with it followed by whitespace is ALLOWED
      immediately, skipping the ask patterns.
    - ``ask``: list of regexes; a match requires interactive approval.

    Evaluation order for commands: deny (custom + built-in) -> allow_prefixes ->
    ask (custom + built-in) -> default allow. So a denied pattern can never be
    bypassed by an allow prefix.

    ``path_rules`` are screened on the file tools' ``path``, on the search tools'
    path/pattern arguments, and on path-like operands of shell commands, so a
    ``**/*.env`` deny rule covers ``read_file(".env")``, ``ls`` /``grep`` /``glob``
    targeting it, and ``cat .env`` alike.

    **Residual gap:** argument screening cannot see a command's *results*. A broad
    search (``grep '' .``) or an expanded wildcard still surfaces content from
    denied paths, because the denied path never appears as a literal argument.
    Path rules are a guardrail against casual/accidental access, not a
    confinement boundary — use ``readonly`` mode or the OS sandbox for that.
    """

    def __init__(
        self,
        mode: str = "smart",
        tool_rules: dict[str, str] | None = None,
        path_rules: dict[str, list[str]] | None = None,
        bash_rules: dict[str, list[str]] | None = None,
        approval_handler: ApprovalHandler | None = None,
    ):
        self._mode = mode
        self._tool_rules = tool_rules or {}
        self._path_rules = path_rules or {}
        self._bash_rules = bash_rules or {}
        self._approval_handler = approval_handler
        self._deny_paths = self._path_rules.get("deny", [])
        self._ask_paths = self._path_rules.get("ask", [])
        self._deny_bash = [re.compile(p) for p in self._bash_rules.get("deny", [])]
        self._ask_bash = [re.compile(p) for p in self._bash_rules.get("ask", [])]
        self._allow_prefixes = [
            p.strip() for p in self._bash_rules.get("allow_prefixes", []) if p and p.strip()
        ]

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def approval_handler(self) -> "ApprovalHandler | None":
        return self._approval_handler

    def check_tool(self, tool_name: str) -> PermissionDecision:
        if tool_name in self._tool_rules:
            return PermissionDecision(self._tool_rules[tool_name])
        if self._mode == "auto" or self._mode == "yolo":
            return PermissionDecision.ALLOW
        if self._mode == "readonly":
            if tool_name in READONLY_DENIED_TOOLS:
                return PermissionDecision.DENY
            return PermissionDecision.ALLOW
        return PermissionDecision.ALLOW

    def _path_matches(self, path: str, pattern: str) -> bool:
        if fnmatch.fnmatch(path, pattern):
            return True
        normalized = pattern.removeprefix("**/")
        return fnmatch.fnmatch(path, normalized) or Path(path).name == normalized

    def _match_path_rules(self, path: str) -> PermissionDecision | None:
        for pattern in self._deny_paths:
            if self._path_matches(path, pattern):
                return PermissionDecision.DENY
        for pattern in self._ask_paths:
            if self._path_matches(path, pattern):
                return PermissionDecision.ASK
        return None

    def check_path(self, path: str, operation: str) -> PermissionDecision:
        if self._mode in ("auto", "yolo"):
            return PermissionDecision.ALLOW
        rule = self._match_path_rules(path)
        if rule:
            return rule
        if self._mode == "readonly" and operation in ("write", "patch"):
            return PermissionDecision.DENY
        return PermissionDecision.ALLOW

    def _check_paths(self, paths: list[str], operation: str) -> PermissionDecision:
        """Strictest ``check_path`` decision across several paths (ALLOW if none)."""
        decision = PermissionDecision.ALLOW
        for path in paths:
            if path:
                decision = _strictest(decision, self.check_path(path, operation))
        return decision

    def check_command(self, command: str) -> PermissionDecision:
        if self._mode in ("auto", "yolo"):
            return PermissionDecision.ALLOW
        if self._mode == "readonly":
            return PermissionDecision.DENY
        for pattern in self._deny_bash + DENY_COMMAND_PATTERNS:
            if pattern.search(command):
                return PermissionDecision.DENY
        if self._matches_allow_prefix(command):
            return PermissionDecision.ALLOW
        for pattern in self._ask_bash + ASK_COMMAND_PATTERNS:
            if pattern.search(command):
                return PermissionDecision.ASK
        return PermissionDecision.ALLOW

    def _matches_allow_prefix(self, command: str) -> bool:
        stripped = command.lstrip()
        for prefix in self._allow_prefixes:
            if stripped == prefix:
                return True
            if stripped.startswith(prefix) and stripped[len(prefix)].isspace():
                # Only fast-path an allow-prefix when the remainder is plain args.
                # A chained/redirected/substituted tail (git status && curl … | bash)
                # must fall through to the deny/ask patterns instead of being allowed.
                if not _SHELL_CHAIN_RE.search(stripped[len(prefix):]):
                    return True
        return False

    async def evaluate_tool_call(self, tool_name: str, arguments: dict) -> tuple[bool, str | None]:
        tool_decision = self.check_tool(tool_name)
        if tool_decision == PermissionDecision.DENY:
            return False, f"Permission denied for tool: {tool_name}"

        # A tool-level rule (e.g. tool_rules={"bash": "ask"}) and the command/path
        # screen are combined with the STRICTER winning — the command screen must
        # never silently downgrade a configured tool-level ASK to ALLOW.
        detail_decision = PermissionDecision.ALLOW
        if tool_name in COMMAND_TOOLS:
            command = arguments.get(COMMAND_TOOLS[tool_name], "") or ""
            # Both screens apply: the regex screen catches dangerous *commands*,
            # the path screen catches dangerous *operands* (`cat .env`).
            detail_decision = _strictest(
                self.check_command(command),
                self._check_paths(command_path_tokens(command), "read"),
            )
        elif tool_name in SEARCH_TOOLS:
            detail_decision = self._check_paths(
                [str(arguments.get(arg)) for arg in SEARCH_TOOLS[tool_name] if arguments.get(arg)],
                "read",
            )
        elif tool_name in WRITE_TOOLS | READ_TOOLS:
            operation = "write" if tool_name in WRITE_TOOLS else "read"
            detail_decision = self.check_path(arguments.get("path", ""), operation)
        decision = _strictest(tool_decision, detail_decision)

        if decision == PermissionDecision.DENY:
            return False, f"Permission denied for {tool_name}"
        if decision == PermissionDecision.ASK:
            action = f"{tool_name}({arguments})"
            if self._approval_handler is None:
                return False, f"Approval required but no handler configured: {action}"
            approved = await self._approval_handler(action)
            if not approved:
                return False, f"User denied: {action}"
        return True, None
