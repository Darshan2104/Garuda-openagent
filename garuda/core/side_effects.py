"""What the agent changed, and putting back what it should not have left running.

An agent is judged on the state of the workspace after it stops, not on the
state during the run. A background server started for a quick test is invisible
in the transcript and fatal in the result: whoever inspects the box next finds
the port taken and the process alive.

The ledger records mutations as they happen and sweeps agent-started processes
before the completion gate runs — so verification observes the same workspace an
outside observer would, rather than one propped up by processes about to vanish.
"""

import asyncio
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

# Where a backgrounding command records the process group it leaves behind.
# Everything it started is in that group, which makes it a precise handle rather
# than a guess reconstructed from the command text.
#
# A file, not stdout, for two reasons. A launch that does *not* redirect its
# output holds the shell's pipe open, so the tool's captured stdout is lost to
# the drain timeout — and that is exactly the launch that most needs sweeping.
# And keeping the marker off stdout means the model's view of the command output
# is byte-for-byte what the command produced. Under /tmp, like background task
# logs, so it never shows up in a `git diff` of the workspace.
LAUNCH_DIR = "/tmp/garuda-launch"

# Self-contained so it still works inside docker/remote where the Garuda package
# may not be installed. ``os.kill(-pid, sig)`` is the portable process-group
# form; dash's builtin ``kill -$pid`` is not.
_KILL_TREE_PY = """
import os, signal, subprocess, sys
sig = getattr(signal, "SIG" + sys.argv[1])
seen = set()

def hit(pid):
    if pid in seen or pid <= 1:
        return
    seen.add(pid)
    for target in (-pid, pid):
        try:
            os.kill(target, sig)
        except OSError:
            pass
    for flag in ("-P", "-g"):
        try:
            out = subprocess.check_output(
                ["pgrep", flag, str(pid)], text=True, stderr=subprocess.DEVNULL
            )
        except Exception:
            out = ""
        for raw in out.split():
            try:
                hit(int(raw))
            except ValueError:
                pass

for raw in sys.argv[2:]:
    try:
        hit(int(raw))
    except ValueError:
        pass
"""


def kill_tree_command(signal_name: str, *pids: str) -> str:
    """Shell command that signals each pid, its group, children, and group mates."""
    nums = [p for p in pids if isinstance(p, str) and p.isdigit() and p not in {"0", "1"}]
    if not nums:
        return "true"
    args = " ".join(shlex.quote(part) for part in (signal_name, *nums))
    return f"python3 -c {shlex.quote(_KILL_TREE_PY)} {args}"


def wrap_launch(command: str, path: str) -> str:
    """``command`` plus a probe recording the launching shell's pid and group.

    Appended on its own line so the command itself is untouched — it still sets
    the exit status, which is restored after the probe runs. `ps` is asked for
    the group rather than it being assumed from ``$$``: whether the shell *leads*
    its group depends on the backend, and sweeping a group we do not lead would
    reach processes that are not ours.
    """
    return (
        f"{command}\n"
        f"__garuda_rc=$?\n"
        f"mkdir -p {shlex.quote(LAUNCH_DIR)} 2>/dev/null\n"
        f"printf '%s %s\\n' \"$$\" \"$(ps -o pgid= -p $$ 2>/dev/null | tr -d ' ')\" "
        f"> {shlex.quote(path)} 2>/dev/null\n"
        f"exit $__garuda_rc"
    )


def parse_launch(text: str) -> dict[str, str] | None:
    """``{"pid", "pgid"}`` from a probe's output, or None if it did not run."""
    parts = (text or "").split()
    if len(parts) < 2 or not (parts[0].isdigit() and parts[1].isdigit()):
        return None
    return {"pid": parts[0], "pgid": parts[1]}


async def read_launch(env: Any, path: str) -> dict[str, str] | None:
    """Collect and remove one launch record. Never raises."""
    try:
        result = await env.execute(
            f"cat {shlex.quote(path)} 2>/dev/null; rm -f {shlex.quote(path)} 2>/dev/null",
            timeout=15.0,
        )
    except Exception:
        logger.debug("Could not read launch record %s", path, exc_info=True)
        return None
    return parse_launch(result.stdout or "")

# A shell that has already returned may have forked a child that has not reached
# exec() yet, so the very first probe can legitimately see nothing. Wait this long
# and look again before concluding a launch left nothing behind.
SWEEP_SETTLE_SEC = 0.5

# Grace between a signal and the probe that checks whether it worked.
SWEEP_TERM_GRACE_SEC = 0.6
SWEEP_KILL_GRACE_SEC = 0.4

# One probe of the process table for `pattern`, printing only plausible targets.
#
# Two exclusions matter and neither is optional:
#   * `$$`/`$PPID` — this very shell was invoked with `pattern` inside its own
#     argv, so `pgrep -f` matches it. Without this the sweep "finds" itself,
#     reports a kill, and declares a workspace clean that still has a server on it.
#   * pid 1 — never signal the container's init.
# The `pgrep` case-match drops the transient forks of this pipeline for the same
# reason; an agent-started process whose command line contains "pgrep" is not a
# case worth keeping the false positives for.
_PROBE_SCRIPT = """\
for p in $(pgrep -f {pattern} 2>/dev/null | head -40); do
  [ "$p" = "$$" ] && continue
  [ "$p" = "$PPID" ] && continue
  [ "$p" = "1" ] && continue
  c=$(ps -o command= -p "$p" 2>/dev/null)
  case "$c" in *pgrep*) continue;; esac
  echo "$p"
done"""

# Everything still in the launch's process group. `ps -Ao pid=,pgid=` is in POSIX
# and reads the kernel's own record of group membership, so nothing here depends
# on what a process called itself. The probing shell is excluded as before: it
# is in its own group, but a backend that does not give it one would otherwise
# make the sweep a candidate for its own kill list.
_GROUP_PROBE_SCRIPT = """\
ps -Ao pid=,pgid= 2>/dev/null | while read -r p g; do
  [ "$g" = {pgid} ] || continue
  [ "$p" = "$$" ] && continue
  [ "$p" = "$PPID" ] && continue
  [ "$p" = "1" ] && continue
  echo "$p"
done"""


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

    A fallback, not the primary handle. Reconstructing a pattern from the command
    text assumes the process's argv still resembles what was typed, and it often
    does not: on macOS ``/usr/bin/python3 server.py`` re-execs as
    ``.../Python.app/Contents/MacOS/Python server.py``, which the pattern
    ``python3 server.py`` cannot match — the process is left running and the
    sweep reports a clean workspace. Process groups are exact; this is for
    launches that leave the group (``nohup``, ``setsid``, daemonisers).

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
    _seen_patterns: set[tuple[str | None, str | None]] = field(default_factory=set)

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
        # The group is the real handle; the pattern is a fallback for launches
        # that leave it. Either alone is a reason to record, so a command too
        # vague to pattern-match is still swept by group.
        pgid = self._launch_pgid(result)
        if pgid is None and pattern is None:
            return
        key = (pgid, pattern)
        if key in self._seen_patterns:
            return
        self._seen_patterns.add(key)
        self.processes.append(
            {"command": command.strip()[:300], "pattern": pattern, "pgid": pgid}
        )

    @staticmethod
    def _launch_pgid(result: ToolResult) -> str | None:
        """The process group of the shell that ran the command, when it led one.

        Declined when the shell is not its own group leader: the group then
        belongs to something else — a persistent shell serving every command of
        the run, or the harness itself — and sweeping it would kill far more than
        this launch. Falling back to the pattern is the lesser failure.
        """
        launch = (result.metadata or {}).get("launch")
        if not isinstance(launch, dict):
            return None
        pid, pgid = launch.get("pid"), launch.get("pgid")
        if not (isinstance(pgid, str) and pgid.isdigit() and pgid not in ("0", "1")):
            return None
        return pgid if pgid == pid else None

    # -- reporting ---------------------------------------------------------

    @property
    def has_pending(self) -> bool:
        return bool(self.processes)

    def summary(self) -> dict[str, Any]:
        return {
            "files_written": sorted(self.files_written),
            "processes_started": [p["command"] for p in self.processes],
            "processes_killed": [s["pattern"] for s in self.swept if s.get("killed")],
            # Signalled and still alive at the last probe. Kept separate from
            # `processes_killed` so "we tried" is never read as "it is gone".
            "processes_surviving": [s["pattern"] for s in self.swept if s.get("survivors")],
            "listeners_after": self.listeners_after,
        }

    def render(self) -> str:
        """Human-readable cleanup report for the transcript."""
        lines = ["[cleanup] Pre-completion sweep of side effects created during this run:"]
        if not self.processes:
            lines.append("  - no background processes were started by you")
        for entry in self.swept:
            if entry.get("survivors"):
                state = "STILL RUNNING after TERM and KILL"
            elif entry.get("killed"):
                state = "terminated"
            else:
                state = "already stopped"
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

        Probe, signal, and then *probe again*: a single pre-kill `pgrep` answers
        the wrong question. It cannot see a child that has not reached exec() yet
        (the launching shell returns first), and finding a pid is not evidence
        the signal landed — a process that ignores TERM, or a group whose leader
        dies while a child keeps the port, both read as "killed" to a one-shot
        sweep. Absence is only ever established by looking after the fact.

        Best-effort by construction: a workspace where cleanup fails is still
        worth verifying, and an exception here must not become a task failure.
        """
        for entry in self.processes[:MAX_SWEEP_TARGETS]:
            self.swept.append({**entry, **await self._sweep_one(env, entry)})
        # Only worth a round trip when something was actually running; on most
        # runs there is nothing to sweep and nothing to report.
        if any(e.get("killed") or e.get("survivors") for e in self.swept):
            self.listeners_after = await self._listeners(env)

    async def _sweep_one(self, env: Any, entry: dict[str, Any]) -> dict[str, Any]:
        """Drive one launch to confirmed absence, or report what survived."""
        pgid, pattern = entry.get("pgid"), entry.get("pattern")
        try:
            pids = await self._probe(env, pgid, pattern)
            if not pids:
                # Nothing yet — but "yet" is the operative word for a launch that
                # has only just been forked. Look once more before believing it.
                await asyncio.sleep(SWEEP_SETTLE_SEC)
                pids = await self._probe(env, pgid, pattern)
            if not pids:
                return {"killed": False, "survivors": []}
            for signal_name, grace in (
                ("TERM", SWEEP_TERM_GRACE_SEC),
                ("KILL", SWEEP_KILL_GRACE_SEC),
                ("KILL", SWEEP_KILL_GRACE_SEC),
            ):
                targets = list(pids)
                if pgid:
                    targets.append(pgid)
                await self._signal(env, targets, signal_name)
                await asyncio.sleep(grace)
                pids = await self._probe(env, pgid, pattern)
                if not pids:
                    return {"killed": True, "survivors": []}
            # Signalled and still there: say so rather than claiming a clean box.
            return {"killed": True, "survivors": pids}
        except Exception:
            logger.debug("Sweep failed for %s / %s", pgid, pattern, exc_info=True)
            return {"killed": False, "survivors": [], "error": True}

    async def _probe(
        self, env: Any, pgid: str | None, pattern: str | None
    ) -> list[str]:
        """PIDs still alive from this launch, by group and then by pattern.

        Both sources are consulted because they fail in opposite directions. The
        group is exact but empty for a launch that left it (``setsid``, ``nohup``,
        a self-daemonising server); the pattern survives that but only matches
        when the process's argv still resembles the command that was typed.
        """
        found: list[str] = []
        if pgid:
            found.extend(await self._probe_group(env, pgid))
        if pattern:
            found.extend(await self._probe_pattern(env, pattern))
        # Order-preserving dedupe: the same pid can answer to both probes.
        return list(dict.fromkeys(found))

    async def _probe_group(self, env: Any, pgid: str) -> list[str]:
        """PIDs still in the launching shell's process group.

        No argv matching: membership of that group *is* the evidence that this
        run started the process, so an interpreter that rewrote its own command
        line is caught exactly like one that did not.
        """
        result = await env.execute(_GROUP_PROBE_SCRIPT.format(pgid=shlex.quote(pgid)), timeout=15.0)
        return [p for p in (result.stdout or "").split() if p.isdigit() and p != "1"]

    async def _probe_pattern(self, env: Any, pattern: str) -> list[str]:
        """PIDs currently matching ``pattern``, excluding the probe's own shell."""
        result = await env.execute(
            _PROBE_SCRIPT.format(pattern=shlex.quote(pattern)), timeout=15.0
        )
        return [p for p in (result.stdout or "").split() if p.isdigit() and p != "1"]

    async def _signal(self, env: Any, pids: list[str], signal_name: str) -> None:
        """Signal each pid and its process group, ignoring the ones already gone."""
        await env.execute(kill_tree_command(signal_name, *pids), timeout=30.0)

    async def _listeners(self, env: Any) -> str:
        for probe in _LISTENER_PROBES:
            try:
                result = await env.execute(probe, timeout=15.0)
            except Exception:
                continue
            if result.exit_code == 0 and (result.stdout or "").strip():
                return (result.stdout or "").strip()[:2000]
        return ""
