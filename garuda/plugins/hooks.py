"""Hook registry: programmatic and config-loadable lifecycle/tool hooks.

Hooks come from two places:

1. Programmatic registration (``register_before_tool`` etc.) — used by SDK callers.
2. YAML settings files (``.garuda/settings.yaml`` in the workspace, plus the
   global ``~/.garuda/settings.yaml``) declaring shell-command hooks::

       hooks:
         before_tool:
           - match: "bash"            # tool-name glob (fnmatch), default "*"
             command: "./check.sh"    # receives JSON event on stdin
         after_tool:
           - match: "*"
             command: "echo done >> /tmp/log"
         session_start:
           - command: "notify-send 'garuda run started'"
         session_end:
           - command: "./cleanup.sh"

Shell-command hooks receive the JSON-serialized event on stdin and run with a
30s timeout. For ``before_tool`` hooks an exit code of 2 blocks the tool call;
any other nonzero exit code is logged and the call is allowed. Hook errors and
timeouts never crash the agent run.
"""

import asyncio
import fnmatch
import json
import logging
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from garuda.types import ToolCall, ToolResult

logger = logging.getLogger(__name__)

BeforeToolHook = Callable[[ToolCall, dict[str, Any]], Awaitable[ToolCall | None]]
AfterToolHook = Callable[[ToolCall, ToolResult, dict[str, Any]], Awaitable[ToolResult | None]]
SessionHook = Callable[[dict[str, Any]], Awaitable[None]]

COMMAND_TIMEOUT_SECONDS = 30.0
BLOCK_EXIT_CODE = 2


async def _run_hook_command(
    command: str,
    event: dict[str, Any],
    timeout: float | None = None,
) -> int | None:
    """Run a shell hook command with the JSON event on stdin.

    Returns the exit code, or ``None`` if the command failed to start or
    timed out. Never raises.
    """
    if timeout is None:
        timeout = COMMAND_TIMEOUT_SECONDS
    try:
        process = await asyncio.create_subprocess_shell(
            command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except Exception as exc:
        logger.warning("Hook command %r failed to start: %s: %s", command, type(exc).__name__, exc)
        return None
    try:
        payload = json.dumps(event, default=str).encode("utf-8")
        await asyncio.wait_for(process.communicate(payload), timeout=timeout)
    except asyncio.TimeoutError:
        logger.warning("Hook command %r timed out after %ss", command, timeout)
        try:
            process.kill()
            await process.wait()
        except ProcessLookupError:
            pass
        return None
    except Exception as exc:
        logger.warning("Hook command %r failed: %s: %s", command, type(exc).__name__, exc)
        return None
    return process.returncode


def _command_before_tool_hook(pattern: str, command: str) -> BeforeToolHook:
    async def hook(call: ToolCall, context: dict[str, Any]) -> ToolCall | None:
        if not fnmatch.fnmatch(call.name, pattern):
            return call
        event = {
            "event": "before_tool",
            "tool": call.name,
            "arguments": call.arguments,
            "session_id": context.get("session_id"),
        }
        exit_code = await _run_hook_command(command, event)
        if exit_code == BLOCK_EXIT_CODE:
            logger.warning("Hook command %r blocked tool %s (exit code 2)", command, call.name)
            return None
        if exit_code not in (0, None):
            logger.warning(
                "Hook command %r exited %s for tool %s; allowing the call",
                command,
                exit_code,
                call.name,
            )
        return call

    return hook


def _command_after_tool_hook(pattern: str, command: str) -> AfterToolHook:
    async def hook(
        call: ToolCall,
        result: ToolResult,
        context: dict[str, Any],
    ) -> ToolResult | None:
        if not fnmatch.fnmatch(call.name, pattern):
            return None
        event = {
            "event": "after_tool",
            "tool": call.name,
            "arguments": call.arguments,
            "result": result.content,
            "is_error": result.is_error,
            "session_id": context.get("session_id"),
        }
        exit_code = await _run_hook_command(command, event)
        if exit_code not in (0, None):
            logger.warning("Hook command %r exited %s after tool %s", command, exit_code, call.name)
        return None

    return hook


def _command_session_hook(command: str) -> SessionHook:
    async def hook(event: dict[str, Any]) -> None:
        exit_code = await _run_hook_command(command, event)
        if exit_code not in (0, None):
            logger.warning(
                "Hook command %r exited %s for %s", command, exit_code, event.get("event")
            )

    return hook


@dataclass
class HookRegistry:
    before_tool: list[BeforeToolHook] = field(default_factory=list)
    after_tool: list[AfterToolHook] = field(default_factory=list)
    session_start: list[SessionHook] = field(default_factory=list)
    session_end: list[SessionHook] = field(default_factory=list)

    def register_before_tool(self, hook: BeforeToolHook) -> None:
        self.before_tool.append(hook)

    def register_after_tool(self, hook: AfterToolHook) -> None:
        self.after_tool.append(hook)

    def register_session_start(self, hook: SessionHook) -> None:
        self.session_start.append(hook)

    def register_session_end(self, hook: SessionHook) -> None:
        self.session_end.append(hook)

    async def run_before_tool(self, call: ToolCall, context: dict[str, Any]) -> ToolCall | None:
        current = call
        for hook in self.before_tool:
            try:
                updated = await hook(current, context)
            except Exception as exc:
                logger.warning(
                    "before_tool hook failed (%s: %s); continuing", type(exc).__name__, exc
                )
                continue
            if updated is None:
                return None
            current = updated
        return current

    async def run_after_tool(
        self,
        call: ToolCall,
        result: ToolResult,
        context: dict[str, Any],
    ) -> ToolResult:
        current = result
        for hook in self.after_tool:
            try:
                updated = await hook(call, current, context)
            except Exception as exc:
                logger.warning(
                    "after_tool hook failed (%s: %s); continuing", type(exc).__name__, exc
                )
                continue
            if updated is not None:
                current = updated
        return current

    async def on_session_start(self, task: str, session_id: str) -> None:
        """Fire session-start hooks. Never raises."""
        event = {"event": "session_start", "task": task, "session_id": session_id}
        await self._fire(self.session_start, event)

    async def on_session_end(self, result_summary: dict[str, Any]) -> None:
        """Fire session-end hooks with a result summary dict. Never raises."""
        event = {"event": "session_end", **(result_summary or {})}
        await self._fire(self.session_end, event)

    async def _fire(self, hooks: list[SessionHook], event: dict[str, Any]) -> None:
        for hook in hooks:
            try:
                await hook(event)
            except Exception as exc:
                logger.warning(
                    "%s hook failed (%s: %s); continuing",
                    event.get("event", "session"),
                    type(exc).__name__,
                    exc,
                )

    @classmethod
    def from_config(cls, path: str | Path) -> "HookRegistry":
        """Build a registry from a YAML settings file (see module docstring)."""
        registry = cls()
        registry.extend_from_config(path)
        return registry

    def extend_from_config(self, path: str | Path) -> None:
        """Append shell-command hooks declared in a YAML settings file."""
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        hooks_config = data.get("hooks") or {}
        for entry in hooks_config.get("before_tool") or []:
            if not entry.get("command"):
                continue
            self.before_tool.append(
                _command_before_tool_hook(entry.get("match", "*"), entry["command"])
            )
        for entry in hooks_config.get("after_tool") or []:
            if not entry.get("command"):
                continue
            self.after_tool.append(
                _command_after_tool_hook(entry.get("match", "*"), entry["command"])
            )
        for entry in hooks_config.get("session_start") or []:
            if not entry.get("command"):
                continue
            self.session_start.append(_command_session_hook(entry["command"]))
        for entry in hooks_config.get("session_end") or []:
            if not entry.get("command"):
                continue
            self.session_end.append(_command_session_hook(entry["command"]))


def global_settings_path() -> Path:
    override = os.environ.get("GARUDA_GLOBAL_SETTINGS")
    if override:
        return Path(override).expanduser()
    from garuda.config.agent_home import global_home_dir

    return global_home_dir() / "settings.yaml"


def build_hook_registry(workspace_root: str | Path | None = None) -> HookRegistry:
    """Build a HookRegistry from global then project settings files.

    Loads the global ``settings.yaml`` (``~/.agent`` standard, ``~/.garuda``
    back-compat; override path with ``GARUDA_GLOBAL_SETTINGS``) first, then the
    project ``<workspace>/.agent/settings.yaml`` (and ``.garuda`` back-compat) —
    project hook lists are merged after (and thus run after) the global ones.
    """
    from garuda.config.agent_home import resolve_agent_home

    registry = HookRegistry()
    paths = [global_settings_path()]
    if workspace_root:
        for root in resolve_agent_home(workspace_root).roots:
            paths.append(root / "settings.yaml")
    for path in paths:
        if not path.is_file():
            continue
        try:
            registry.extend_from_config(path)
        except Exception as exc:
            logger.warning(
                "Failed to load hooks config %s (%s: %s); skipping",
                path,
                type(exc).__name__,
                exc,
            )
    return registry
