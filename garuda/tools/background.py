"""Background process tools: start long-running commands, poll output, kill.

Implemented through ``env.execute`` (setsid + log file under /tmp/garuda-tasks/),
so the same mechanism works in local, docker, and remote environments. Logs live
in /tmp — NOT the workspace — so they never pollute a ``git diff`` / glob of the
project. A capture monitor drains inherited stdout to EOF and publishes a receipt;
polling requires that receipt and process-group settlement before completion.
State is keyed by (session_id, task_id) because registry tool instances
are shared across sessions.

**Not supported under bubblewrap.** bwrap gives each exec its own PID namespace
and ``--die-with-parent``, so a backgrounded process cannot outlive the launcher
that started it. Rather than hand back a pid that names nothing, the tool refuses
on that backend — see :data:`BWRAP_UNSUPPORTED`.
"""

import asyncio
import contextlib
import json
import logging
import os
import shlex
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path

from garuda.core.side_effects import apply_kill_tree, kill_tree_command
from garuda.tools.protocol import ToolContext, ToolEffect
from garuda.types import ToolResult
from garuda.workspace.protocol import Environment

logger = logging.getLogger(__name__)

# Kept out of the workspace so background logs don't show up as untracked files.
TASKS_DIR = "/tmp/garuda-tasks"
MAX_OUTPUT_BYTES = 20_000

# The capture owner remains alive until all inherited stdout handles close.
# Launcher exit alone says nothing about a descendant still holding the pipe.
_CAPTURE_MONITOR = r"""import hashlib, json, os, stat, subprocess, sys
child = subprocess.Popen(['sh', '-c', sys.argv[1]], stdin=subprocess.DEVNULL,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
digest = hashlib.sha256()
captured = 0
while block := os.read(child.stdout.fileno(), 65536):
    sys.stdout.buffer.write(block)
    sys.stdout.buffer.flush()
    digest.update(block)
    captured += len(block)
code = child.wait()
sys.stdout.buffer.flush()
os.fsync(sys.stdout.fileno())
info = os.fstat(sys.stdout.fileno())
if not stat.S_ISREG(info.st_mode) or info.st_size != captured:
    raise OSError('capture log changed before completion')
receipt = {'schema': 'garuda.background-capture', 'version': 1,
           'snapshot': [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns],
           'sha256': digest.hexdigest()}
data = json.dumps(receipt, sort_keys=True, separators=(',', ':')).encode()
directory = int(sys.argv[3])
temporary = sys.argv[2] + '.tmp'
fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
try:
    position = 0
    while position < len(data):
        count = os.write(fd, data[position:])
        if count <= 0:
            raise OSError('capture receipt write made no progress')
        position += count
    os.fsync(fd)
finally:
    os.close(fd)
os.replace(temporary, sys.argv[2], src_dir_fd=directory, dst_dir_fd=directory)
raise SystemExit(code)
"""

_CAPTURE_READ = r"""import hashlib, json, os, re, stat, sys

def snapshot(info):
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns]

def unique(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError('duplicate receipt field')
        value[key] = item
    return value

def receipt(directory, name):
    try:
        fd = os.open(name + '.complete', os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=directory)
    except FileNotFoundError:
        return None
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_size > 1024:
            raise ValueError('unsafe capture receipt')
        raw = os.read(fd, 1025)
        if len(raw) != before.st_size or snapshot(os.fstat(fd)) != snapshot(before):
            raise ValueError('unstable capture receipt')
    finally:
        os.close(fd)
    value = json.loads(raw, object_pairs_hook=unique)
    if (not isinstance(value, dict) or set(value) != {'schema', 'version', 'snapshot', 'sha256'}
            or value['schema'] != 'garuda.background-capture'
            or type(value['version']) is not int or value['version'] != 1
            or not isinstance(value['snapshot'], list) or len(value['snapshot']) != 5
            or any(type(number) is not int or number < 0 for number in value['snapshot'])
            or not isinstance(value['sha256'], str) or not re.fullmatch('[0-9a-f]{64}', value['sha256'])):
        raise ValueError('invalid capture receipt')
    return value

def read_capture():
    expected_binding = json.loads(sys.argv[5])
    if (not isinstance(expected_binding, list) or len(expected_binding) != 2
            or any(not isinstance(pair, list) or len(pair) != 2
                   or any(type(number) is not int or number < 0 for number in pair)
                   or pair[1] <= 0 for pair in expected_binding)):
        raise ValueError('invalid capture launch binding')
    directory = os.open(os.path.dirname(sys.argv[2]), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        directory_info = os.fstat(directory)
        if [directory_info.st_dev, directory_info.st_ino] != expected_binding[0]:
            raise ValueError('capture directory does not match its launch binding')
        pid = int(sys.argv[1])
        if pid <= 1:
            raise ValueError('invalid capture owner')
        try:
            os.killpg(pid, 0)
        except ProcessLookupError:
            state = 'UNSETTLED'
        except OSError:
            state = 'UNKNOWN'
        else:
            state = 'RUNNING'
        name = os.path.basename(sys.argv[2])
        commitment = receipt(directory, name)
        fd = os.open(name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=directory)
        try:
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode):
                raise ValueError('capture log is not a regular file')
            if [before.st_dev, before.st_ino] != expected_binding[1]:
                raise ValueError('capture log does not match its launch binding')
            limit = max(1, min(int(sys.argv[4]), int(sys.argv[3])))
            remaining = min(limit, before.st_size)
            expected = remaining
            os.lseek(fd, before.st_size - remaining, os.SEEK_SET)
            data = bytearray()
            while remaining:
                block = os.read(fd, remaining)
                if not block:
                    raise ValueError('capture log was truncated')
                data.extend(block)
                remaining -= len(block)
            if snapshot(os.fstat(fd)) != snapshot(before):
                raise ValueError('capture log changed during reading')
            if commitment is not None:
                if commitment['snapshot'] != snapshot(before):
                    raise ValueError('capture log does not match its receipt')
                if expected == before.st_size and hashlib.sha256(data).hexdigest() != commitment['sha256']:
                    raise ValueError('capture digest does not match its receipt')
                if state == 'UNSETTLED':
                    state = 'EXITED'
            return state, before.st_size, data
        finally:
            os.close(fd)
    finally:
        os.close(directory)

try:
    state, size, data = read_capture()
except FileNotFoundError:
    sys.stdout.buffer.write(b'UNKNOWN\nMISSING\n')
except (OSError, ValueError, TypeError, KeyError, UnicodeError, RecursionError):
    sys.stdout.buffer.write(b'UNKNOWN\nUNSAFE\n')
else:
    sys.stdout.buffer.write((state + '\nSIZE ' + str(size) + '\n').encode() + data)
"""

_CAPTURE_LAUNCH = r"""import json, os, subprocess, sys
parent = os.path.dirname(sys.argv[2])
os.makedirs(parent, mode=0o700, exist_ok=True)
directory = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
try:
    name = os.path.basename(sys.argv[2])
    for existing in (name + '.complete', name + '.complete.tmp'):
        try:
            os.stat(existing, dir_fd=directory, follow_symlinks=False)
        except FileNotFoundError:
            continue
        raise FileExistsError('background capture paths already exist')
    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
    try:
        directory_info, log_info = os.fstat(directory), os.fstat(fd)
        if any(info.st_dev < 0 or info.st_ino <= 0 for info in (directory_info, log_info)):
            raise OSError('background capture identity is unavailable')
        child = subprocess.Popen([sys.executable, '-u', '-c', sys.argv[3], sys.argv[1],
                                  name + '.complete', str(directory)], stdout=fd, stderr=subprocess.STDOUT,
                                 stdin=subprocess.DEVNULL, start_new_session=True, pass_fds=(directory,))
    finally:
        os.close(fd)
finally:
    os.close(directory)
print(json.dumps({'schema': 'garuda.background-launch', 'version': 1, 'pid': child.pid,
                  'name': name, 'directory': [directory_info.st_dev, directory_info.st_ino],
                  'log': [log_info.st_dev, log_info.st_ino]}, sort_keys=True, separators=(',', ':')))
"""

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
    """Portable shell that signals a pid, its process group, and descendants."""
    return kill_tree_command(signal_name, pid)


async def _deliver_kill(env: Environment, pid: str, signal_name: str, timeout: float = 10.0) -> None:
    if type(env).__name__ == "LocalEnvironment":
        apply_kill_tree(signal_name, pid)
        return
    await env.execute(_kill_process_group(pid, signal_name), timeout=timeout)


async def reap_session(session_id: str, env: Environment) -> int:
    """Kill any still-running background tasks for a session (called at run end).

    Prevents host orphans in the local workspace where nothing else tears the
    process down; for docker/remote the container teardown also handles it, so
    this is best-effort and never raises.
    """
    keys = [k for k in _TASKS if k[0] == session_id]
    await reap_session_verified(session_id, env)
    return len(keys)


async def reap_session_verified(session_id: str, env: Environment) -> list[str]:
    """Kill a session's background tasks; return the pids whose death is unproven.

    Local tasks lead their own process group, so a group with no members left
    is proof. A group that still answers after the kill, or one this process
    may not signal, is reported. Docker and remote tasks die with their
    container and are not reported here. Never raises.
    """
    survivors: list[str] = []
    for key in [k for k in list(_TASKS) if k[0] == session_id]:
        task = _TASKS.pop(key, None)
        if task is None:
            continue
        try:
            await _deliver_kill(env, task.pid, "KILL", timeout=10.0)
            if task.process is not None:
                with contextlib.suppress(ProcessLookupError, TimeoutError, asyncio.TimeoutError):
                    await asyncio.wait_for(task.process.wait(), timeout=2.0)
        except Exception:
            logger.debug("Failed to reap background task %s", task.task_id, exc_info=True)
        if type(env).__name__ == "LocalEnvironment" and not await _group_gone(task.pid):
            survivors.append(task.pid)
    return survivors


async def _group_gone(pid: str, attempts: int = 20) -> bool:
    try:
        group = int(pid)
    except ValueError:
        return False
    for _ in range(attempts):
        try:
            os.killpg(group, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            pass  # macOS reports a group of unreaped zombies this way
        except OSError:
            return False
        # A killed leader that is our own child stays a zombie (and keeps its
        # group answering) until it is reaped; reap it if nobody else has.
        with contextlib.suppress(ChildProcessError, OSError):
            os.waitpid(group, os.WNOHANG)
        await asyncio.sleep(0.1)
    return False


@dataclass
class BackgroundTask:
    task_id: str
    pid: str
    command: str
    log_path: str
    # LocalEnvironment spawns keep the asyncio handle so kill can wait without
    # leaving a zombie; remote/docker launches only have a pid string.
    process: asyncio.subprocess.Process | None = None
    directory_identity: tuple[int, int] | None = None
    log_identity: tuple[int, int] | None = None


_TASKS: dict[tuple[str, str], BackgroundTask] = {}


def _capture_identity(value) -> tuple[int, int] | None:
    if (isinstance(value, (list, tuple)) and len(value) == 2
            and all(type(number) is int and number >= 0 for number in value)
            and value[1] > 0):
        return tuple(value)
    return None


async def _spawn_local(
    env: Environment, command: str, log_path: str
) -> tuple[str, asyncio.subprocess.Process, tuple[int, int], tuple[int, int]]:
    """Start ``command`` as its own session leader in this process.

    Avoids the shell ``&`` / ``$!`` path: on Ubuntu that path recorded the wrong
    pid when nested ``setsid`` forked, and group kills then missed grandchildren.
    """
    Path(TASKS_DIR).mkdir(mode=0o700, parents=True, exist_ok=True)
    directory = os.open(Path(log_path).parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        name = Path(log_path).name
        for existing in (name + ".complete", name + ".complete.tmp"):
            try:
                os.stat(existing, dir_fd=directory, follow_symlinks=False)
            except FileNotFoundError:
                continue
            raise FileExistsError("background capture receipt already exists")
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
        try:
            parent_info, log_info = os.fstat(directory), os.fstat(fd)
            directory_identity = _capture_identity((parent_info.st_dev, parent_info.st_ino))
            log_identity = _capture_identity((log_info.st_dev, log_info.st_ino))
            if directory_identity is None or log_identity is None:
                raise OSError("background capture identity is unavailable")
            process = await asyncio.create_subprocess_exec(
                sys.executable, "-u", "-c", _CAPTURE_MONITOR, command, name + ".complete", str(directory),
                cwd=getattr(env, "workspace_root", None), stdout=fd,
                stderr=asyncio.subprocess.STDOUT, stdin=asyncio.subprocess.DEVNULL,
                start_new_session=True, pass_fds=(directory,),
            )
        finally:
            os.close(fd)
    finally:
        os.close(directory)
    return str(process.pid), process, directory_identity, log_identity


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
    effect = ToolEffect.MUTATING
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
        process = None
        directory_identity = log_identity = None
        if type(env).__name__ == "LocalEnvironment":
            try:
                pid, process, directory_identity, log_identity = await _spawn_local(env, command, log_path)
            except OSError as exc:
                return ToolResult(
                    tool_call_id="",
                    content=f"Failed to start background task: {exc}",
                    is_error=True,
                )
        else:
            # The portable launcher opens its log safely, starts the capture
            # owner as a session leader, and returns that exact child's pid.
            launcher = (
                f"python3 -c {shlex.quote(_CAPTURE_LAUNCH)} {shlex.quote(command)} "
                f"{shlex.quote(log_path)} {shlex.quote(_CAPTURE_MONITOR)}"
            )
            result = await env.execute(launcher, timeout=15.0)
            pid = ""
            try:
                from garuda.core.session_records import parse_json

                reply = result.stdout.strip().splitlines()[-1].encode("utf-8")
                if result.exit_code != 0 or result.truncated or len(reply) > 1024:
                    raise ValueError("unavailable background launch receipt")
                launch = parse_json(reply)
                if (set(launch) != {"schema", "version", "pid", "name", "directory", "log"}
                        or launch["schema"] != "garuda.background-launch"
                        or type(launch["version"]) is not int or launch["version"] != 1
                        or type(launch["pid"]) is not int or launch["pid"] <= 1
                        or launch["name"] != Path(log_path).name):
                    raise ValueError("invalid background launch receipt")
                directory_identity = _capture_identity(launch["directory"])
                log_identity = _capture_identity(launch["log"])
                if directory_identity is None or log_identity is None:
                    raise ValueError("missing background launch binding")
                pid = str(launch["pid"])
            except (ValueError, TypeError, KeyError, IndexError, UnicodeError):
                pass
            if not pid:
                return ToolResult(
                    tool_call_id="",
                    content=(
                        f"Failed to start background task: "
                        f"{result.stderr or result.stdout}"
                    ),
                    is_error=True,
                )
        _TASKS[_task_key(ctx, task_id)] = BackgroundTask(
            task_id=task_id,
            pid=pid,
            command=command,
            log_path=log_path,
            process=process,
            directory_identity=directory_identity,
            log_identity=log_identity,
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
    effect = ToolEffect.READ_ONLY
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
        python = shlex.quote(sys.executable) if type(env).__name__ == "LocalEnvironment" else "python3"
        directory_identity = _capture_identity(task.directory_identity)
        log_identity = _capture_identity(task.log_identity)
        if directory_identity is None or log_identity is None:
            from garuda.core.artifact_read_model import source_capture

            return ToolResult(tool_call_id="", is_error=True,
                content=f"Task {task.task_id} output is unavailable: its launch binding is unverified.",
                metadata={**_task_state(ctx), "artifact_source": source_capture(
                    kind="background_tail", completeness="failed", identity=task.task_id, reason="capture_failed",
                )})
        binding = json.dumps([directory_identity, log_identity], separators=(",", ":"))
        probe = await env.execute(
            f"{python} -c {shlex.quote(_CAPTURE_READ)} {shlex.quote(task.pid)} "
            f"{shlex.quote(task.log_path)} {tail_bytes} {MAX_OUTPUT_BYTES} {shlex.quote(binding)}",
            timeout=15.0,
        )
        lines = probe.stdout.split("\n", 2)
        status = lines[0].strip() if lines else "UNKNOWN"
        size_line = lines[1].strip() if len(lines) > 1 else "MISSING"
        output = lines[2] if len(lines) > 2 else ""
        state = {"RUNNING": "still running", "EXITED": "exited"}.get(
            status, "awaiting confirmed output settlement",
        )
        from garuda.core.artifact_read_model import source_capture

        if size_line in {"MISSING", "UNSAFE"}:
            source = source_capture(
                kind="background_tail", completeness="failed",
                identity=task.task_id,
                reason="background_log_missing" if size_line == "MISSING" else "capture_failed",
            )
            return ToolResult(
                tool_call_id="",
                content=f"Task {task.task_id} output is unavailable: its captured log is missing or unsafe to read.",
                is_error=True,
                metadata={**_task_state(ctx), "artifact_source": source},
            )
        try:
            total_bytes = int(size_line.removeprefix("SIZE "))
            if total_bytes < 0 or not size_line.startswith("SIZE "):
                raise ValueError("invalid background byte count")
        except ValueError:
            total_bytes = None
        if status == "EXITED" and total_bytes is not None and not probe.truncated and probe.exit_code == 0:
            # Only a safe, stable log snapshot plus settled capture-owner evidence
            # permits retirement; unsafe input remains available for reconciliation.
            _TASKS.pop(_task_key(ctx, arguments["task_id"]), None)
        # Environment text decoding can expand a two-byte UTF-8 suffix into
        # multiple replacement characters. Re-encoding that text is not a byte
        # counter for the source log. Keep decoder/backend loss uncertain.
        decoding_unknown = "\ufffd" in output or probe.truncated or probe.exit_code != 0
        omitted = max(0, total_bytes - tail_bytes) if total_bytes is not None else None
        if decoding_unknown:
            omitted = None
        complete = status == "EXITED" and omitted == 0
        source = source_capture(
            kind="background_tail",
            completeness="complete" if complete else "partial",
            omitted_source_bytes=omitted,
            identity=task.task_id,
            reason=None if complete else "running_or_tail_limited",
        )
        return ToolResult(
            tool_call_id="",
            content=f"Task {task.task_id} ({task.command}) is {state}.\n--- output tail ---\n{output}",
            metadata={**_task_state(ctx), "artifact_source": source},
        )


class KillTaskTool:
    effect = ToolEffect.MUTATING
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
        # Collect-then-signal in apply_kill_tree; TERM then KILL/KILL to match the
        # sweep (a second KILL catches children forked between the first two
        # signals, and zombies are filtered by the probes as already-dead).
        await _deliver_kill(env, task.pid, "TERM", timeout=15.0)
        if type(env).__name__ == "LocalEnvironment":
            await asyncio.sleep(0.2)
        await _deliver_kill(env, task.pid, "KILL", timeout=15.0)
        if type(env).__name__ == "LocalEnvironment":
            await asyncio.sleep(0.2)
        await _deliver_kill(env, task.pid, "KILL", timeout=15.0)
        if task.process is not None:
            with contextlib.suppress(ProcessLookupError):
                await asyncio.wait_for(task.process.wait(), timeout=2.0)
        _TASKS.pop(key, None)
        return ToolResult(
            tool_call_id="",
            content=f"Killed background task {task.task_id} (pid {task.pid}).",
            metadata=_task_state(ctx),
        )
