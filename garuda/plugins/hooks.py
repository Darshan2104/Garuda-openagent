"""Hook registry: programmatic and config-loadable lifecycle/tool hooks.

Hooks come from two places:

1. Programmatic registration (``register_before_tool`` etc.) — used by SDK callers.
2. YAML settings files (``.agent/settings.yaml``, ``.garuda/settings.yaml`` back-compat)
   declaring shell-command hooks::

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
30s timeout (``timeout:`` per hook, at most 300s, and never past the run's
deadline). Each runs in its own process group, which is killed on timeout or
cancellation so a hook cannot leave work running.

A ``before_tool`` hook is a guard, so it **fails closed**: exit code 0 allows the
call, exit code 2 blocks it, and anything else — a nonzero exit, a command that
cannot start, a timeout, or an exception in a programmatic hook — blocks it too
(logged as ``hook.failed_blocked``). A hook in the user's own global settings can
opt out with ``on_failure: allow`` to make it advisory; a project's hooks always
fail closed. ``after_tool`` and session hooks cannot block anything, so their
failures are logged and the run continues.

The GLOBAL settings file (``~/.agent/settings.yaml``) always loads. A PROJECT's
own settings file only loads its hook commands when the global settings opt in
via ``trust_project_hooks: true`` — see :func:`build_hook_registry` and the
trust-boundary note in :mod:`garuda.config.agent_home`.
"""

import asyncio
import fnmatch
import json
import logging
import os
import signal
import time
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
MAX_HOOK_TIMEOUT_SECONDS = 300.0
BLOCK_EXIT_CODE = 2
FAILED_BLOCKED = "hook.failed_blocked"
ON_FAILURE_VALUES = ("block", "allow")


def _kill_group(process) -> None:
    """Kill the hook's whole process group, so its children cannot outlive it."""
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def _remaining(context: dict[str, Any] | None) -> float | None:
    deadline = (context or {}).get("deadline_monotonic")
    if deadline is None:
        return None
    return deadline - time.monotonic()


async def _run_hook_command(
    command: str,
    event: dict[str, Any],
    timeout: float | None = None,
) -> int | None:
    """Run a shell hook command with the JSON event on stdin.

    Returns the exit code, or ``None`` if the command failed to start or
    timed out. Never raises, except to propagate cancellation after the hook's
    process group has been killed.
    """
    if timeout is None:
        timeout = COMMAND_TIMEOUT_SECONDS
    timeout = min(timeout, MAX_HOOK_TIMEOUT_SECONDS)
    if timeout <= 0:
        logger.warning("Hook command %r not run: the run's deadline has passed", command)
        return None
    try:
        process = await asyncio.create_subprocess_shell(
            command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception as exc:
        logger.warning("Hook command %r failed to start: %s: %s", command, type(exc).__name__, exc)
        return None
    try:
        payload = json.dumps(event, default=str).encode("utf-8")
        await asyncio.wait_for(process.communicate(payload), timeout=timeout)
    except asyncio.TimeoutError:
        logger.warning("Hook command %r timed out after %ss", command, timeout)
        _kill_group(process)
        await process.wait()
        return None
    except asyncio.CancelledError:
        _kill_group(process)
        await process.wait()
        raise
    except Exception as exc:
        logger.warning("Hook command %r failed: %s: %s", command, type(exc).__name__, exc)
        _kill_group(process)
        return None
    return process.returncode


def _command_before_tool_hook(
    pattern: str,
    command: str,
    *,
    on_failure: str = "block",
    timeout: float | None = None,
) -> BeforeToolHook:
    async def hook(call: ToolCall, context: dict[str, Any]) -> ToolCall | None:
        if not fnmatch.fnmatch(call.name, pattern):
            return call
        event = {
            "event": "before_tool",
            "tool": call.name,
            "arguments": call.arguments,
            "session_id": context.get("session_id"),
        }
        budget = timeout if timeout is not None else COMMAND_TIMEOUT_SECONDS
        remaining = _remaining(context)
        if remaining is not None:
            budget = min(budget, remaining)
        exit_code = await _run_hook_command(command, event, timeout=budget)
        if exit_code == 0:
            return call
        if exit_code == BLOCK_EXIT_CODE:
            logger.warning("Hook command %r blocked tool %s (exit code 2)", command, call.name)
            return None
        outcome = "did not finish" if exit_code is None else f"exited {exit_code}"
        if on_failure == "allow":
            logger.warning(
                "Advisory hook command %r %s for tool %s; allowing the call",
                command,
                outcome,
                call.name,
            )
            return call
        logger.warning(
            "%s: guard hook command %r %s for tool %s; blocking the call",
            FAILED_BLOCKED,
            command,
            outcome,
            call.name,
        )
        return None

    return hook


def _advisory(hook: BeforeToolHook) -> BeforeToolHook:
    """Wrap a programmatic guard so a crash allows the call instead of blocking."""

    async def wrapped(call: ToolCall, context: dict[str, Any]) -> ToolCall | None:
        try:
            return await hook(call, context)
        except Exception as exc:
            logger.warning(
                "Advisory before_tool hook failed (%s: %s); allowing the call",
                type(exc).__name__,
                exc,
            )
            return call

    return wrapped


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

    def register_before_tool(self, hook: BeforeToolHook, *, on_failure: str = "block") -> None:
        """Add a guard. It fails closed unless registered with ``on_failure="allow"``."""
        if on_failure not in ON_FAILURE_VALUES:
            raise ValueError(f"on_failure must be one of {ON_FAILURE_VALUES}, got {on_failure!r}")
        self.before_tool.append(_advisory(hook) if on_failure == "allow" else hook)

    def register_after_tool(self, hook: AfterToolHook) -> None:
        self.after_tool.append(hook)

    def register_session_start(self, hook: SessionHook) -> None:
        self.session_start.append(hook)

    def register_session_end(self, hook: SessionHook) -> None:
        self.session_end.append(hook)

    async def run_before_tool(self, call: ToolCall, context: dict[str, Any]) -> ToolCall | None:
        """Run every guard in order; ``None`` means the call is blocked.

        A guard that raises, or returns something that is not a tool call,
        blocks the call. The caller must re-authorize a call a guard rewrote.
        """
        current = call
        for hook in self.before_tool:
            remaining = _remaining(context)
            if remaining is not None and remaining <= 0:
                logger.warning(
                    "%s: the run's deadline passed before every guard ran; blocking %s",
                    FAILED_BLOCKED,
                    call.name,
                )
                return None
            try:
                updated = await hook(current, context)
            except Exception as exc:
                logger.warning(
                    "%s: before_tool hook failed (%s: %s); blocking the call",
                    FAILED_BLOCKED,
                    type(exc).__name__,
                    exc,
                )
                return None
            if updated is None:
                return None
            if not isinstance(updated, ToolCall) or not isinstance(updated.arguments, dict):
                logger.warning(
                    "%s: before_tool hook returned a malformed call; blocking it", FAILED_BLOCKED
                )
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

    def extend_from_config(self, path: str | Path, *, allow_advisory: bool = True) -> None:
        """Append shell-command hooks declared in a YAML settings file.

        ``allow_advisory`` is False for a project's own settings: only the user's
        global file may make a guard advisory with ``on_failure: allow``.
        """
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        hooks_config = data.get("hooks") or {}
        for entry in hooks_config.get("before_tool") or []:
            if not entry.get("command"):
                continue
            self.before_tool.append(
                _command_before_tool_hook(
                    entry.get("match", "*"),
                    entry["command"],
                    on_failure=_on_failure(entry, path, allow_advisory),
                    timeout=_hook_timeout(entry, path),
                )
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


def _on_failure(entry: dict, path: str | Path, allow_advisory: bool) -> str:
    value = entry.get("on_failure", "block")
    if value not in ON_FAILURE_VALUES:
        logger.warning(
            "Hook in %s has on_failure %r (expected block or allow); it fails closed", path, value
        )
        return "block"
    if value == "allow" and not allow_advisory:
        logger.warning(
            "Ignoring on_failure: allow for a project hook in %s; project guards fail closed",
            path,
        )
        return "block"
    return value


def _hook_timeout(entry: dict, path: str | Path) -> float | None:
    value = entry.get("timeout")
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        logger.warning("Hook in %s has an invalid timeout %r; using the default", path, value)
        return None
    if value > MAX_HOOK_TIMEOUT_SECONDS:
        logger.warning(
            "Hook in %s asks for a %ss timeout; capping it at %ss",
            path,
            value,
            MAX_HOOK_TIMEOUT_SECONDS,
        )
        return MAX_HOOK_TIMEOUT_SECONDS
    return float(value)


def global_settings_path() -> Path:
    from garuda.config.agent_home import global_settings_path as _global_settings_path

    return _global_settings_path()


def build_hook_registry(workspace_root: str | Path | None = None) -> HookRegistry:
    """Build a HookRegistry from the global settings file, plus project settings
    files if the user has explicitly trusted them.

    The global ``settings.yaml`` (``~/.agent`` standard, ``~/.garuda`` back-compat;
    override path with ``GARUDA_GLOBAL_SETTINGS``) always loads — it's user-authored.
    Project ``<workspace>/.agent/settings.yaml`` (and ``.garuda`` back-compat) can
    declare hook commands too, but a *cloned repo's own config* must not be able to
    self-authorize running arbitrary shell commands at session start / around every
    tool call — so project hook commands only load when the user has opted in
    globally via ``trust_project_hooks: true`` (mirrors ``load_project_tools``; see
    :mod:`garuda.config.agent_home`). When present but untrusted, they're skipped
    with a warning rather than silently dropped.
    """
    from garuda.config.agent_home import resolve_agent_home

    registry = HookRegistry()
    global_path = global_settings_path()
    if global_path.is_file():
        try:
            registry.extend_from_config(global_path)
        except Exception as exc:
            logger.warning(
                "Failed to load hooks config %s (%s: %s); skipping",
                global_path,
                type(exc).__name__,
                exc,
            )

    if workspace_root:
        home = resolve_agent_home(workspace_root)
        project_paths = [root / "settings.yaml" for root in home.roots]
        project_paths = [p for p in project_paths if p.is_file()]
        if project_paths and not home.trust_project_hooks:
            logger.warning(
                "Ignoring hook commands in %s: project-declared shell-command hooks "
                "are untrusted by default (a cloned repo could otherwise run arbitrary "
                "commands at session start). Set trust_project_hooks: true in the "
                "global settings.yaml (%s) to enable them for workspaces you trust.",
                ", ".join(str(p) for p in project_paths),
                global_path,
            )
        else:
            for path in project_paths:
                try:
                    registry.extend_from_config(path, allow_advisory=False)
                except Exception as exc:
                    logger.warning(
                        "Failed to load hooks config %s (%s: %s); skipping",
                        path,
                        type(exc).__name__,
                        exc,
                    )
    return registry
