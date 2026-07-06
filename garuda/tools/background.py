"""Background process tools: start long-running commands, poll output, kill.

Implemented through ``env.execute`` (setsid + log file under /tmp/garuda-tasks/),
so the same mechanism works in local, docker, and remote environments. Logs live
in /tmp — NOT the workspace — so they never pollute a ``git diff`` / glob of the
project. State is keyed by (session_id, task_id) because registry tool instances
are shared across sessions.
"""

import logging
import shlex
import uuid
from dataclasses import dataclass

from garuda.tools.protocol import ToolContext
from garuda.types import ToolResult
from garuda.workspace.protocol import Environment

logger = logging.getLogger(__name__)

# Kept out of the workspace so background logs don't show up as untracked files.
TASKS_DIR = "/tmp/garuda-tasks"
MAX_OUTPUT_BYTES = 20_000


async def reap_session(session_id: str, env: Environment) -> int:
    """Kill any still-running background tasks for a session (called at run end).

    Prevents host orphans in the local workspace where nothing else tears the
    process down; for docker/remote the container teardown also handles it, so
    this is best-effort and never raises.
    """
    keys = [k for k in list(_TASKS) if k[0] == session_id]
    for key in keys:
        task = _TASKS.pop(key, None)
        if task is None:
            continue
        try:
            await env.execute(
                f"kill -KILL -{task.pid} 2>/dev/null; kill -KILL {task.pid} 2>/dev/null || true",
                timeout=10.0,
            )
        except Exception:
            logger.debug("Failed to reap background task %s", task.task_id, exc_info=True)
    return len(keys)


@dataclass
class BackgroundTask:
    task_id: str
    pid: str
    command: str
    log_path: str


_TASKS: dict[tuple[str, str], BackgroundTask] = {}


def _task_key(ctx: ToolContext, task_id: str) -> tuple[str, str]:
    return (ctx.session_id, task_id)


class BashBackgroundTool:
    name = "bash_background"
    description = (
        "Start a long-running command in the background (servers, watchers, slow builds). "
        "Returns a task_id; use task_output to poll its output and kill_task to stop it. "
        "Output is captured to a log file under /tmp (not the workspace)."
    )
    parameters = {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "Shell command to run in the background"},
        },
        "required": ["command"],
    }

    async def execute(self, arguments: dict, env: Environment, ctx: ToolContext) -> ToolResult:
        command = arguments["command"]
        task_id = uuid.uuid4().hex[:8]
        log_path = f"{TASKS_DIR}/{task_id}.log"
        # Use setsid where available (Linux, containers) so the command is its own
        # session/process-group leader and kill_task can reap the whole tree via
        # `kill -- -$pid`. macOS ships no setsid, so fall back to a plain background
        # launch there. ';' not '&&' so the whole chain isn't backgrounded (which
        # would hold the launcher's stdout pipe open until it exits).
        launcher = (
            f"mkdir -p {shlex.quote(TASKS_DIR)}; "
            f"if command -v setsid >/dev/null 2>&1; then _s=setsid; else _s=; fi; "
            f"$_s sh -c {shlex.quote(command)} > {shlex.quote(log_path)} 2>&1 < /dev/null & echo $!"
        )
        result = await env.execute(launcher, timeout=15.0)
        pid = result.stdout.strip().splitlines()[-1].strip() if result.stdout.strip() else ""
        if result.exit_code != 0 or not pid.isdigit():
            return ToolResult(
                tool_call_id="",
                content=f"Failed to start background task: {result.stderr or result.stdout}",
                is_error=True,
            )
        _TASKS[_task_key(ctx, task_id)] = BackgroundTask(
            task_id=task_id, pid=pid, command=command, log_path=log_path
        )
        return ToolResult(
            tool_call_id="",
            content=(
                f"Started background task {task_id} (pid {pid}): {command}\n"
                f"Poll with task_output(task_id=\"{task_id}\")."
            ),
        )


class TaskOutputTool:
    name = "task_output"
    description = (
        "Read the captured output of a background task started with bash_background, "
        "and report whether it is still running."
    )
    parameters = {
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "description": "Task id returned by bash_background"},
            "tail_bytes": {
                "type": "integer",
                "description": f"Max bytes of output to return from the end (default {MAX_OUTPUT_BYTES})",
                "default": MAX_OUTPUT_BYTES,
            },
        },
        "required": ["task_id"],
    }

    async def execute(self, arguments: dict, env: Environment, ctx: ToolContext) -> ToolResult:
        task = _TASKS.get(_task_key(ctx, arguments["task_id"]))
        if task is None:
            return ToolResult(
                tool_call_id="",
                content=f"Unknown background task: {arguments['task_id']}",
                is_error=True,
            )
        tail_bytes = int(arguments.get("tail_bytes", MAX_OUTPUT_BYTES))
        probe = await env.execute(
            f"kill -0 {task.pid} 2>/dev/null && echo RUNNING || echo EXITED; "
            f"tail -c {tail_bytes} {shlex.quote(task.log_path)} 2>/dev/null",
            timeout=15.0,
        )
        lines = probe.stdout.splitlines()
        status = lines[0].strip() if lines else "UNKNOWN"
        output = "\n".join(lines[1:])
        state = "still running" if status == "RUNNING" else "exited"
        return ToolResult(
            tool_call_id="",
            content=f"Task {task.task_id} ({task.command}) is {state}.\n--- output tail ---\n{output}",
        )


class KillTaskTool:
    name = "kill_task"
    description = "Stop a background task started with bash_background."
    parameters = {
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "description": "Task id returned by bash_background"},
        },
        "required": ["task_id"],
    }

    async def execute(self, arguments: dict, env: Environment, ctx: ToolContext) -> ToolResult:
        key = _task_key(ctx, arguments["task_id"])
        task = _TASKS.get(key)
        if task is None:
            return ToolResult(
                tool_call_id="",
                content=f"Unknown background task: {arguments['task_id']}",
                is_error=True,
            )
        # Negative pid targets the whole process group (setsid leader + children);
        # the plain-pid KILL is a fallback for the leader itself.
        await env.execute(
            f"kill -TERM -{task.pid} 2>/dev/null; sleep 0.2; "
            f"kill -KILL -{task.pid} 2>/dev/null; kill -KILL {task.pid} 2>/dev/null || true",
            timeout=15.0,
        )
        _TASKS.pop(key, None)
        return ToolResult(
            tool_call_id="",
            content=f"Killed background task {task.task_id} (pid {task.pid}).",
        )
