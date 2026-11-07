"""Explicitly enabled debugging tools; no arbitrary DAP dispatch surface."""

import asyncio
import json

from garuda.runtime.debug import UNSUPPORTED, DebugError
from garuda.runtime.process_session import CleanupUnproved
from garuda.tools.protocol import ToolEffect, positive_int
from garuda.types import ToolResult
from garuda.workspace.local import LocalEnvironment
from garuda.workspace.protocol import WorkspaceAccessDenied
from garuda.workspace.settlement import settle_task


def _character_limit(value):
    return positive_int(value) or ("must be at least 1024" if value < 1024 else None)


class _DebugTool:
    explicit_only = True

    async def execute(self, arguments, env, ctx):
        if type(env) is not LocalEnvironment:
            return ToolResult("", "Debugging supports local workspaces only; Docker, remote, "
                              "sandbox and tmux are unsupported.", is_error=True)
        if self.name == "debug_inspect" and arguments.get("operation") == "variables":
            return ToolResult("", "Variable rendering requires mutating debug_variables authority; "
                              "debugpy can execute Python repr methods.", is_error=True)
        if self.name == "debug_variables" and arguments.get("operation") != "variables":
            return ToolResult("", UNSUPPORTED, is_error=True)
        service = env.debug_service()
        try:
            timeout = None
            if ctx.deadline_monotonic is not None:
                timeout = max(0, ctx.deadline_monotonic - asyncio.get_running_loop().time())
            async with asyncio.timeout(timeout):
                if self.name == "debug_launch":
                    caps = {"max_variables": 100, "max_depth": 3, "max_chars": 12_000,
                            "output_chars": 20_000}
                    limits = {}
                    for name, cap in caps.items():
                        value = ctx.option(self.name, name, cap)
                        if positive_int(value):
                            raise DebugError(f"{name} must be a positive integer")
                        limits[name] = min(value, cap)
                    if _character_limit(limits["max_chars"]):
                        raise DebugError("max_chars must be at least 1024")
                    result = await service.launch(ctx.session_id, arguments, limits)
                elif self.name in {"debug_inspect", "debug_variables"}:
                    result = await service.inspect(ctx.session_id, arguments)
                else:
                    result = await service.control(ctx.session_id, arguments)
            return ToolResult("", json.dumps(result, ensure_ascii=True), metadata={"debug": result})
        except (asyncio.CancelledError, TimeoutError) as failed:
            # Cancellation settles every resource issued to this agent session,
            # including a launch that has not yet returned a debug session id.
            survivors = await settle_task(asyncio.create_task(service.reap(ctx.session_id)))
            if survivors:
                cleanup_error = DebugError("cleanup unproved: " + "; ".join(survivors))
                if isinstance(failed, asyncio.CancelledError):
                    raise failed from cleanup_error
                raise cleanup_error from failed
            raise
        except (DebugError, CleanupUnproved, WorkspaceAccessDenied,
                OSError, ValueError, TypeError, KeyError) as exc:
            return ToolResult("", str(exc)[:2000], is_error=True)


class DebugLaunchTool(_DebugTool):
    name = "debug_launch"
    effect = ToolEffect.MUTATING
    description = (
        "Launch a workspace Python file under debugpy (local only). Runs code and is screened "
        "like bash. Returns debug session identity, capabilities, breakpoints and stop state. "
        "No attach, evaluate, getters, writes, memory or raw DAP requests."
    )
    parameters = {
        "type": "object", "additionalProperties": False,
        "properties": {"program": {"type": "string"},
                       "args": {"type": "array", "items": {"type": "string"}, "maxItems": 100},
                       "breakpoints": {"type": "array", "items": {"type": "integer", "minimum": 1},
                                       "maxItems": 100}},
        "required": ["program"],
    }
    options_schema = {name: positive_int for name in (
        "max_variables", "max_depth", "max_chars", "output_chars")}
    options_schema["max_chars"] = _character_limit


class DebugInspectTool(_DebugTool):
    name = "debug_inspect"
    effect = ToolEffect.READ_ONLY
    description = (
        "Inspect status, captured output, stackTrace or scopes of an owned debug session. "
        "Use returned handles for scopes; handles expire on resume. "
        "Variables require debug_variables because adapter rendering may execute Python repr. "
        "Debug observations do not establish acceptance."
    )
    parameters = {
        "type": "object", "additionalProperties": False,
        "properties": {"debug_session_id": {"type": "string"},
                       "operation": {"enum": ["status", "output", "stackTrace", "scopes"]},
                       "handle": {"type": "string"}, "count": {"type": "integer", "minimum": 1},
                       "depth": {"type": "integer", "minimum": 1}},
        "required": ["debug_session_id", "operation"],
    }


class DebugVariablesTool(_DebugTool):
    name = "debug_variables"
    effect = ToolEffect.MUTATING
    description = (
        "Read bounded variables of an owned stopped debug session using a scope/container handle. "
        "Rendering can execute user Python repr methods, so this requires mutating authority. "
        "Only builtin containers can be expanded; no evaluate, getters, setters or raw requests."
    )
    parameters = {
        **DebugInspectTool.parameters,
        "properties": {**DebugInspectTool.parameters["properties"],
                       "operation": {"enum": ["variables"]}},
        "required": ["debug_session_id", "operation", "handle"],
    }


class DebugControlTool(_DebugTool):
    name = "debug_control"
    effect = ToolEffect.MUTATING
    description = (
        "Run next, stepIn, stepOut or continue in an owned debug session; disconnect/terminate "
        "kill and verify the owned adapter/debuggee session. " + UNSUPPORTED
    )
    parameters = {
        "type": "object", "additionalProperties": False,
        "properties": {"debug_session_id": {"type": "string"},
                       "operation": {"enum": ["next", "stepIn", "stepOut", "continue",
                                              "disconnect", "terminate"]}},
        "required": ["debug_session_id", "operation"],
    }
