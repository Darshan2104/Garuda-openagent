import asyncio
import re
import shlex
import time
import uuid
from pathlib import Path

from garuda.types import ExecResult

MARKER_PREFIX = "__CMDEND__"

# The fragment that appears in the *typed* command line. The single-quoted
# split ('__CMD''END__') guarantees the typed line never contains the literal
# assembled marker, so polling can only match the printf *output* line.
TYPED_MARKER_FRAGMENT = "'__CMD''END__"


def build_marker_payload(command: str, seq: int) -> str:
    """Wrap a command so its completion emits an assembled marker with exit code.

    The typed line shows ``printf '__CMD''END__%s__%s__\\n' <seq> $?`` which never
    contains the literal ``__CMDEND__`` marker; only the printf output does.
    """
    return f"{command}; printf '__CMD''END__%s__%s__\\n' {seq} $?"


def marker_regex(seq: int) -> re.Pattern[str]:
    """Regex matching the assembled marker output line for a given sequence."""
    return re.compile(rf"{MARKER_PREFIX}{seq}__(\d+)__")


def find_marker(text: str, seq: int) -> tuple[int, int] | None:
    """Return (match_start, exit_code) for the assembled marker, or None."""
    match = marker_regex(seq).search(text)
    if match is None:
        return None
    return match.start(), int(match.group(1))


def pane_delta(before: str, after: str) -> str:
    """Return the pane content added since ``before`` was captured.

    The last line of ``before`` is typically an interactive prompt that gets
    rewritten when keys are sent, so if an exact prefix match fails we retry
    with the final line of ``before`` dropped. If neither prefix matches
    (e.g. the pane scrolled or cleared), fall back to the full capture.
    """
    if after.startswith(before):
        return after[len(before):]
    trimmed = before.rstrip("\n")
    newline = trimmed.rfind("\n")
    prefix = trimmed[: newline + 1] if newline >= 0 else ""
    if prefix and after.startswith(prefix):
        return after[len(prefix):]
    return after


def strip_marker_output(text: str, seq: int) -> tuple[str, int | None]:
    """Strip the typed command echo and marker line from captured output.

    Returns ``(cleaned_output, exit_code)``. ``exit_code`` is None when the
    assembled marker was not found (command still running / timed out).
    """
    found = find_marker(text, seq)
    exit_code: int | None = None
    if found is not None:
        start, exit_code = found
        text = text[:start]
    lines = text.splitlines()
    cleaned = [
        line
        for line in lines
        if TYPED_MARKER_FRAGMENT not in line and MARKER_PREFIX not in line
    ]
    return "\n".join(cleaned).strip("\n"), exit_code


class TmuxEnvironment:
    """Stateful terminal environment backed by a persistent tmux session."""

    def __init__(
        self,
        workspace_root: str | Path | None = None,
        session_name: str | None = None,
        history_limit: int = 5000,
        confine_to_workspace: bool = True,
    ):
        self._workspace_root = Path(workspace_root or Path.cwd()).resolve()
        self._session_name = session_name or f"garuda-{uuid.uuid4().hex[:8]}"
        self._history_limit = history_limit
        self._confine_to_workspace = confine_to_workspace
        self._seq = 0
        self._started = False

    @property
    def workspace_root(self) -> str:
        return str(self._workspace_root)

    @property
    def session_name(self) -> str:
        return self._session_name

    def _resolve_path(self, path: str) -> Path:
        candidate = Path(path)
        if not candidate.is_absolute():
            candidate = self._workspace_root / candidate
        resolved = candidate.resolve()
        if self._confine_to_workspace and not resolved.is_relative_to(self._workspace_root):
            raise PermissionError(
                f"Path {path} is outside the workspace root {self._workspace_root}. "
                "Pass an explicit workspace or disable confinement."
            )
        return resolved

    async def start(self) -> None:
        if self._started:
            return
        self._workspace_root.mkdir(parents=True, exist_ok=True)
        await self._run_tmux(
            [
                "new-session",
                "-d",
                "-s",
                self._session_name,
                "-c",
                str(self._workspace_root),
            ]
        )
        await self._run_tmux(["set-option", "-t", self._session_name, "history-limit", str(self._history_limit)])
        self._started = True

    async def stop(self) -> None:
        if not self._started:
            return
        await self._run_tmux(["kill-session", "-t", self._session_name])
        self._started = False

    async def execute(
        self,
        command: str,
        timeout: float | None = 120.0,
        cwd: str | None = None,
    ) -> ExecResult:
        await self.start()
        if cwd:
            await self.send_command(f"cd {shlex.quote(cwd)}", timeout=10.0, marker_polling=True)
        return await self.send_command(command, timeout=timeout or 120.0, marker_polling=True)

    async def send_command(
        self,
        command: str,
        timeout: float = 120.0,
        marker_polling: bool = True,
    ) -> ExecResult:
        """Type ``command`` into the tmux pane.

        With ``marker_polling`` the command is wrapped so completion emits an
        assembled ``__CMDEND__<seq>__<exit>__`` marker that is polled for; the
        real exit code is returned. Without it (interactive input to a running
        TUI), the raw keys are sent with no marker appended, followed by a
        short settle sleep and a capture.
        """
        await self.start()
        self._seq += 1
        seq = self._seq
        payload = build_marker_payload(command, seq) if marker_polling else command

        before = await self.capture_pane()
        start = time.monotonic()
        await self._run_tmux(["send-keys", "-t", self._session_name, payload, "Enter"])

        if not marker_polling:
            await asyncio.sleep(min(timeout, 1.0))
            pane = await self.capture_pane()
            delta = pane_delta(before, pane)
            return ExecResult(
                stdout=delta,
                stderr="",
                exit_code=0,
                duration_ms=int((time.monotonic() - start) * 1000),
            )

        pane = await self._poll_until_marker(seq, timeout, start)
        delta = pane_delta(before, pane)
        output, exit_code = strip_marker_output(delta, seq)
        duration_ms = int((time.monotonic() - start) * 1000)
        if exit_code is None:
            return ExecResult(
                stdout=output,
                stderr=(
                    f"Command did not complete within {timeout}s; "
                    "it may still be running in the tmux pane."
                ),
                exit_code=124,
                duration_ms=duration_ms,
                truncated=True,
            )
        return ExecResult(
            stdout=output,
            stderr="",
            exit_code=exit_code,
            duration_ms=duration_ms,
        )

    async def capture_pane(self) -> str:
        await self.start()
        output = await self._run_tmux(
            ["capture-pane", "-p", "-J", "-S", "-", "-t", self._session_name],
            capture=True,
        )
        return output

    async def read_file(self, path: str) -> str:
        target = self._resolve_path(path)
        return await asyncio.to_thread(target.read_text, encoding="utf-8")

    async def write_file(self, path: str, content: str) -> None:
        target = self._resolve_path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(target.write_text, content, encoding="utf-8")

    async def _poll_until_marker(self, seq: int, timeout: float, start: float) -> str:
        interval = 0.2
        while time.monotonic() - start < timeout:
            pane = await self.capture_pane()
            if find_marker(pane, seq) is not None:
                return pane
            await asyncio.sleep(interval)
        return await self.capture_pane()

    async def _run_tmux(self, args: list[str], capture: bool = False) -> str:
        process = await asyncio.create_subprocess_exec(
            "tmux",
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        if process.returncode != 0:
            err = stderr.decode(errors="replace").strip()
            raise RuntimeError(f"tmux failed: {' '.join(args)}: {err}")
        if capture:
            return stdout.decode(errors="replace")
        return ""
