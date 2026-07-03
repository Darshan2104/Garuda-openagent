import fnmatch
import re
from enum import Enum
from pathlib import Path
from typing import Awaitable, Callable

ApprovalHandler = Callable[[str], Awaitable[bool]]


class PermissionDecision(str, Enum):
    ALLOW = "allow"
    DENY = "deny"
    ASK = "ask"


DENY_COMMAND_PATTERNS = [
    re.compile(r"rm\s+-rf\s+/", re.IGNORECASE),
    re.compile(r"mkfs\.", re.IGNORECASE),
    re.compile(r"\bdd\s+if=", re.IGNORECASE),
    re.compile(r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;", re.IGNORECASE),
]

ASK_COMMAND_PATTERNS = [
    re.compile(r"\brm\s+-rf\b", re.IGNORECASE),
    re.compile(r"\bsudo\b", re.IGNORECASE),
    re.compile(r"\bchmod\b", re.IGNORECASE),
    re.compile(r"\bcurl\b.*\|\s*bash", re.IGNORECASE),
]

# Tools whose primary argument is a shell command that must be screened.
COMMAND_TOOLS = {
    "bash": "command",
    "bash_background": "command",
    "tmux_exec": "command",
}

# File-operation tools that modify the filesystem.
WRITE_TOOLS = {"write_file", "edit"}

# File-operation tools that only read.
READ_TOOLS = {"read_file", "read_pdf", "read_spreadsheet"}

READONLY_DENIED_TOOLS = {"write_file", "edit", "tmux_exec", "bash_background", "kill_task"}


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
        if tool_name == "task_complete":
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
                return True
        return False

    async def evaluate_tool_call(self, tool_name: str, arguments: dict) -> tuple[bool, str | None]:
        decision = self.check_tool(tool_name)
        if decision == PermissionDecision.DENY:
            return False, f"Permission denied for tool: {tool_name}"

        if tool_name in COMMAND_TOOLS:
            decision = self.check_command(arguments.get(COMMAND_TOOLS[tool_name], ""))
        elif tool_name in WRITE_TOOLS | READ_TOOLS:
            operation = "write" if tool_name in WRITE_TOOLS else "read"
            decision = self.check_path(arguments.get("path", ""), operation)

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
