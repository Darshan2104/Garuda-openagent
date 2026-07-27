"""What the agent changed, and putting back what it should not have left running.

An agent is judged on the state of the workspace after it stops, not on the
state during the run. A background server started for a quick test is invisible
in the transcript and fatal in the result: whoever inspects the box next finds
the port taken and the process alive.

The ledger records mutations as they happen and sweeps agent-started processes
before the completion gate runs — so verification observes the same workspace an
outside observer would, rather than one propped up by processes about to vanish.
"""

import logging
import re
import shlex
from dataclasses import dataclass, field
from typing import Any

from garuda.types import ToolCall, ToolResult

logger = logging.getLogger(__name__)

# Tools whose successful execution mutates the workspace.
_WRITE_TOOLS = frozenset({"write_file", "edit", "multi_edit", "notebook_edit"})

# A command is backgrounded by a trailing `&` (not `&&`), or by a launcher that
# detaches. Matching the trailing form requires care: `a && b` must not match.
_TRAILING_AMP = re.compile(r"(?<!&)&\s*$")
_DETACHERS = re.compile(r"\b(nohup|setsid|disown)\b")

# Long-running servers are frequently started without any backgrounding marker,
# via a launcher that daemonises itself. Recognised so they are swept too.
_DAEMONISERS = re.compile(
    r"\b(nginx|httpd|apache2|redis-server|mongod|mysqld|postgres|dockerd|"
    r"systemctl\s+start|service\s+\S+\s+start)\b"
)

# Commands used to inspect listeners, in preference order.
_LISTENER_PROBES = (
    "ss -ltnp 2>/dev/null",
    "netstat -ltnp 2>/dev/null",
    "lsof -nP -iTCP -sTCP:LISTEN 2>/dev/null",
)

MAX_SWEEP_TARGETS = 24


def _strip_background_markers(command: str) -> str:
    """The command text without the shell syntax that detaches it."""
    text = _TRAILING_AMP.sub("", command).strip()
    text = _DETACHERS.sub("", text).strip()
    return text


def _last_segment(command: str) -> str:
    """The final pipeline stage — the part that is actually left running."""
    parts = re.split(r"&&|\|\||;", command)
    return parts[-1].strip() if parts else command.strip()


def is_backgrounding(command: str) -> bool:
    if not command:
        return False
    return bool(
        _TRAILING_AMP.search(command)
        or _DETACHERS.search(command)
        or _DAEMONISERS.search(command)
    )


def process_pattern(command: str) -> str | None:
    """A ``pgrep -f`` pattern matching just this launch, or None if too vague.

    Deliberately conservative. The pattern must name a concrete program *and* at
    least one argument, because sweeping on a bare interpreter name would kill
    unrelated processes — including, in a local workspace, the agent's own.
    """
    text = _strip_background_markers(_last_segment(command))
    if not text:
        return None
    try:
        tokens = shlex.split(text)
    except ValueError:
        tokens = text.split()
    tokens = [t for t in tokens if not re.match(r"^\w+=", t)]
    while tokens and tokens[0] in ("sudo", "env", "time", "nice", "stdbuf", "exec"):
        tokens = tokens[1:]
    if len(tokens) < 2:
        return None
    program = tokens[0].rsplit("/", 1)[-1]
    # Require a distinguishing argument, e.g. the script path.
    argument = next((t for t in tokens[1:] if not t.startswith("-")), None)
    if not argument:
        return None
    return f"{program} {argument}"


@dataclass
class SideEffectLedger:
    """Mutations performed during a run, and the cleanup they imply."""

    files_written: set[str] = field(default_factory=set)
    processes: list[dict[str, Any]] = field(default_factory=list)
    swept: list[dict[str, Any]] = field(default_factory=list)
    listeners_before: str = ""
    listeners_after: str = ""
    _seen_patterns: set[str] = field(default_factory=set)

    # -- recording ---------------------------------------------------------

    def observe(self, call: ToolCall, result: ToolResult) -> None:
        """Note anything this tool call changed. Never raises."""
        try:
            self._observe(call, result)
        except Exception:  # bookkeeping must never break a turn
            logger.debug("Side-effect bookkeeping failed for %s", call.name, exc_info=True)

    def _observe(self, call: ToolCall, result: ToolResult) -> None:
        name = call.name
        args = call.arguments or {}
        if name in _WRITE_TOOLS:
            if not result.is_error:
                self.files_written.add(str(args.get("path") or args.get("file_path") or ""))
                self.files_written.discard("")
            return
        if name != "bash":
            return
        command = args.get("command")
        if not isinstance(command, str) or not is_backgrounding(command):
            return
        # Deliberately recorded even when the call reported an error. A launch
        # that keeps the shell's stdout open blocks until the tool times out, so
        # the tool reports failure while the process is very much alive — the
        # exact case that most needs sweeping.
        pattern = process_pattern(command)
        if pattern is None or pattern in self._seen_patterns:
            return
        self._seen_patterns.add(pattern)
        self.processes.append({"command": command.strip()[:300], "pattern": pattern})

    # -- reporting ---------------------------------------------------------

    @property
    def has_pending(self) -> bool:
        return bool(self.processes)

    def summary(self) -> dict[str, Any]:
        return {
            "files_written": sorted(self.files_written),
            "processes_started": [p["command"] for p in self.processes],
            "processes_killed": [s["pattern"] for s in self.swept if s.get("killed")],
            "listeners_after": self.listeners_after,
        }

    def render(self) -> str:
        """Human-readable cleanup report for the transcript."""
        lines = ["[cleanup] Pre-completion sweep of side effects created during this run:"]
        if not self.processes:
            lines.append("  - no background processes were started by you")
        for entry in self.swept:
            state = "terminated" if entry.get("killed") else "already stopped"
            lines.append(f"  - {state}: {entry['pattern']}  (from `{entry['command']}`)")
        lines.append(
            "  Verification below runs against the workspace in this state — the state "
            "anyone inspecting it after you would see. If the task needs a process running, "
            "it must be started by a script in the workspace, not left alive by you."
        )
        return "\n".join(lines)

    # -- cleanup -----------------------------------------------------------

    async def sweep(self, env: Any) -> None:
        """Terminate agent-started background processes; record what happened.

        Best-effort by construction: a workspace where cleanup fails is still
        worth verifying, and an exception here must not become a task failure.
        """
        for entry in self.processes[:MAX_SWEEP_TARGETS]:
            pattern = entry["pattern"]
            killed = False
            try:
                probe = await env.execute(
                    f"pgrep -f {shlex.quote(pattern)} 2>/dev/null | head -20", timeout=15.0
                )
                pids = [p for p in (probe.stdout or "").split() if p.isdigit()]
                if pids:
                    # Exclude our own shell and pid 1 so the sweep cannot take
                    # down the container it is running in.
                    joined = " ".join(pids)
                    await env.execute(
                        f"for p in {joined}; do "
                        f'if [ "$p" != "1" ] && [ "$p" != "$$" ]; then '
                        f"kill -TERM $p 2>/dev/null || true; fi; done; "
                        f"sleep 1; "
                        f"for p in {joined}; do "
                        f'if [ "$p" != "1" ] && [ "$p" != "$$" ]; then '
                        f"kill -KILL $p 2>/dev/null || true; fi; done",
                        timeout=30.0,
                    )
                    killed = True
            except Exception:
                logger.debug("Sweep failed for pattern %s", pattern, exc_info=True)
            self.swept.append({**entry, "killed": killed})
        # Only worth a round trip when something was actually running; on most
        # runs there is nothing to sweep and nothing to report.
        if any(entry.get("killed") for entry in self.swept):
            self.listeners_after = await self._listeners(env)

    async def _listeners(self, env: Any) -> str:
        for probe in _LISTENER_PROBES:
            try:
                result = await env.execute(probe, timeout=15.0)
            except Exception:
                continue
            if result.exit_code == 0 and (result.stdout or "").strip():
                return (result.stdout or "").strip()[:2000]
        return ""
