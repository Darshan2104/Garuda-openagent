"""Background process tools: start long-running commands, poll output, kill.

Implemented through ``env.execute`` (setsid + log file under /tmp/garuda-tasks/),
so the same mechanism works in local, docker, and remote environments. Logs live
in /tmp — NOT the workspace — so they never pollute a ``git diff`` / glob of the
project. State is keyed by (session_id, task_id) because registry tool instances
are shared across sessions.

**Not supported under bubblewrap.** bwrap gives each exec its own PID namespace
and ``--die-with-parent``, so a backgrounded process cannot outlive the launcher
that started it. Rather than hand back a pid that names nothing, the tool refuses
on that backend — see :data:`BWRAP_UNSUPPORTED`.
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

BWRAP_UNSUPPORTED = (
    "bash_background is not supported under the bubblewrap sandbox. Each command "
    "runs in its own PID namespace (--unshare-pid --die-with-parent), so a "
    "backgrounded process is PID 1 of a namespace that is torn down as soon as the "
    "launcher returns: the task would die immediately and the returned pid would be "
    "meaningless to task_output/kill_task.\n"
    "Instead: run the command in the foreground with an explicit `timeout`, or use "
    "--workspace-kind docker where background processes persist for the container's "
    "lifetime."
)


def _unsupported_backend(env: Environment) -> str | None:
    """Reason this environment cannot host a background task, or None.

    Refusing up front beats the alternative: bwrap would accept the launch,
    return a namespace-local pid, and reap the process — leaving the agent to
    poll a task that never existed. A clear refusal is recoverable; a phantom
    task id is not.
    """
    backend = getattr(env, "backend", None)
    if backend == "bwrap":
        return BWRAP_UNSUPPORTED
    return None


def _kill_process_group(pid: str, signal_name: str) -> str:
    """Portable shell that signals a pid, its process group, and direct children.

    Uses ``/bin/kill -s SIGNAL --`` so dash (Ubuntu ``/bin/sh``) and bash agree
    on negative-pgid syntax. ``pgrep -P`` catches children when the recorded pid
    was never a group leader — the failure mode that orphans ``sleep`` under a
    killed launcher shell.
    """
    if not pid.isdigit():
        return "true"
    return (
        f"/bin/kill -s {signal_name} -- -{pid} 2>/dev/null || true; "
        f"/bin/kill -s {signal_name} -- {pid} 2>/dev/null || true; "
        f'for c in $(pgrep -P {pid} 2>/dev/null); do '
        f'/bin/kill -s {signal_name} -- "$c" 2>/dev/null || true; done'
    )


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
            await env.execute(_kill_process_group(task.pid, "KILL"), timeout=10.0)
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


def active_task_count(session_id: str) -> int:
    """How many background tasks this session still has running."""
    return sum(1 for key in _TASKS if key[0] == session_id)


def _task_state(ctx: ToolContext) -> dict:
    """Result metadata announcing how many writers are loose in the workspace.

    Every one of these tools returns it, including the polls, because the action
    memo keys its filesystem cache off it: a background build writes between tool
    calls, so a read cached before the launch must not be served after it.
    """
    return {"background_tasks": active_task_count(ctx.session_id)}


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
        unsupported = _unsupported_backend(env)
        if unsupported:
            return ToolResult(tool_call_id="", content=unsupported, is_error=True)
        command = arguments["command"]
        task_id = uuid.uuid4().hex[:8]
        log_path = f"{TASKS_DIR}/{task_id}.log"
        # The task must be its own process-group leader, or `kill -- -$pid` in
        # kill_task/reap_session names some *other* group — the launcher's — and
        # the kill lands on the shell while its children go on running. setsid
        # does this where it exists (Linux, containers); macOS ships none, and
        # there `set -m` gets the same result, because a shell in monitor mode
        # puts each background job in a group of its own. Verified on darwin: an
        # unlaunched-by-either `sleep` outlives the group kill; under `set -m` it
        # does not. ';' not '&&' so the whole chain isn't backgrounded (which
        # would hold the launcher's stdout pipe open until it exits).
        launcher = (
            f"mkdir -p {shlex.quote(TASKS_DIR)}; "
            f"if command -v setsid >/dev/null 2>&1; then _s=setsid; else _s=; set -m; fi; "
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
            metadata=_task_state(ctx),
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
        # Clamp: a model-supplied tail_bytes of 1e9 would pull the whole log into
        # context, and a negative value makes `tail -c` fail outright.
        try:
            tail_bytes = int(arguments.get("tail_bytes", MAX_OUTPUT_BYTES))
        except (TypeError, ValueError):
            tail_bytes = MAX_OUTPUT_BYTES
        tail_bytes = max(1, min(tail_bytes, MAX_OUTPUT_BYTES))
        probe = await env.execute(
            f"kill -0 {task.pid} 2>/dev/null && echo RUNNING || echo EXITED; "
            f"tail -c {tail_bytes} {shlex.quote(task.log_path)} 2>/dev/null",
            timeout=15.0,
        )
        lines = probe.stdout.splitlines()
        status = lines[0].strip() if lines else "UNKNOWN"
        output = "\n".join(lines[1:])
        state = "still running" if status == "RUNNING" else "exited"
        if status == "EXITED":
            # Drop the registry entry once the process is gone. `reap_session` clears
            # a session's tasks at run end, but a long-lived `serve` process that
            # never reaches that path would otherwise accumulate dead entries for
            # its lifetime. The log file is left on disk for a later read.
            _TASKS.pop(_task_key(ctx, arguments["task_id"]), None)
        return ToolResult(
            tool_call_id="",
            content=f"Task {task.task_id} ({task.command}) is {state}.\n--- output tail ---\n{output}",
            metadata=_task_state(ctx),
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
        # Negative pgid targets the whole process group (setsid leader + children);
        # plain-pid and ``pgrep -P`` cover leaders that never owned a group.
        await env.execute(
            f"{_kill_process_group(task.pid, 'TERM')}; sleep 0.2; "
            f"{_kill_process_group(task.pid, 'KILL')}",
            timeout=15.0,
        )
        _TASKS.pop(key, None)
        return ToolResult(
            tool_call_id="",
            content=f"Killed background task {task.task_id} (pid {task.pid}).",
            metadata=_task_state(ctx),
        )
