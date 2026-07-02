"""Background process tools: start long-running commands, poll output, kill.

Implemented through ``env.execute`` (nohup + log file under .garuda-tasks/ in
the workspace), so the same mechanism works in local, docker, and remote
environments. State is keyed by (session_id, task_id) because registry tool
instances are shared across sessions.
"""

import shlex
import uuid
from dataclasses import dataclass

from garuda.tools.protocol import ToolContext
from garuda.types import ToolResult
from garuda.workspace.protocol import Environment

TASKS_DIR = ".garuda-tasks"
MAX_OUTPUT_BYTES = 20_000


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
        "Output is captured to a log file in the workspace."
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
        # NB: ';' not '&&' before nohup — 'A && B &' would background the whole
        # chain and keep the launcher's stdout pipe open until B exits.
        launcher = (
            f"mkdir -p {TASKS_DIR}; "
            f"nohup sh -c {shlex.quote(command)} > {shlex.quote(log_path)} 2>&1 < /dev/null & echo $!"
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
        await env.execute(f"kill {task.pid} 2>/dev/null; sleep 0.2; kill -9 {task.pid} 2>/dev/null || true", timeout=15.0)
        _TASKS.pop(key, None)
        return ToolResult(
            tool_call_id="",
            content=f"Killed background task {task.task_id} (pid {task.pid}).",
        )
