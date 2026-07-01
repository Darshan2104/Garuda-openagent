import re
from enum import Enum
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

READONLY_DENIED_TOOLS = {"write_file", "apply_patch"}


class PermissionEngine:
    def __init__(
        self,
        mode: str = "smart",
        tool_rules: dict[str, str] | None = None,
        approval_handler: ApprovalHandler | None = None,
    ):
        self._mode = mode
        self._tool_rules = tool_rules or {}
        self._approval_handler = approval_handler

    @property
    def mode(self) -> str:
        return self._mode

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

    def check_path(self, path: str, operation: str) -> PermissionDecision:
        if self._mode in ("auto", "yolo"):
            return PermissionDecision.ALLOW
        if self._mode == "readonly" and operation in ("write", "patch"):
            return PermissionDecision.DENY
        return PermissionDecision.ALLOW

    def check_command(self, command: str) -> PermissionDecision:
        if self._mode in ("auto", "yolo"):
            return PermissionDecision.ALLOW
        if self._mode == "readonly":
            return PermissionDecision.DENY
        for pattern in DENY_COMMAND_PATTERNS:
            if pattern.search(command):
                return PermissionDecision.DENY
        for pattern in ASK_COMMAND_PATTERNS:
            if pattern.search(command):
                return PermissionDecision.ASK
        return PermissionDecision.ALLOW

    async def evaluate_tool_call(self, tool_name: str, arguments: dict) -> tuple[bool, str | None]:
        decision = self.check_tool(tool_name)
        if decision == PermissionDecision.DENY:
            return False, f"Permission denied for tool: {tool_name}"

        if tool_name == "bash":
            decision = self.check_command(arguments.get("command", ""))
        elif tool_name == "write_file":
            decision = self.check_path(arguments.get("path", ""), "write")
        elif tool_name == "apply_patch":
            decision = self.check_path(arguments.get("path", ""), "patch")

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
