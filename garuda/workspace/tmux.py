import asyncio
import time
import uuid
from pathlib import Path

from garuda.types import ExecResult


class TmuxEnvironment:
    """Stateful terminal environment backed by a persistent tmux session."""

    def __init__(
        self,
        workspace_root: str | Path | None = None,
        session_name: str | None = None,
        history_limit: int = 5000,
    ):
        self._workspace_root = Path(workspace_root or Path.cwd()).resolve()
        self._session_name = session_name or f"garuda-{uuid.uuid4().hex[:8]}"
        self._history_limit = history_limit
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
        if candidate.is_absolute():
            return candidate
        return self._workspace_root / candidate

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
            await self.send_command(f"cd {cwd}", timeout=10.0, marker_polling=False)
        return await self.send_command(command, timeout=timeout or 120.0, marker_polling=False)

    async def send_command(
        self,
        command: str,
        timeout: float = 120.0,
        marker_polling: bool = True,
    ) -> ExecResult:
        await self.start()
        self._seq += 1
        marker = f"__CMDEND__{self._seq}__"
        if marker_polling:
            payload = f"{command} ; echo '{marker}'"
        else:
            payload = command

        before = await self.capture_pane()
        start = time.monotonic()
        await self._run_tmux(["send-keys", "-t", self._session_name, payload, "Enter"])

        if marker_polling:
            output = await self._poll_until_marker(marker, timeout, start)
        else:
            await asyncio.sleep(min(timeout, 1.0))
            output = await self.capture_pane()

        delta = output[len(before) :] if output.startswith(before) else output
        duration_ms = int((time.monotonic() - start) * 1000)
        return ExecResult(
            stdout=delta,
            stderr="",
            exit_code=0,
            duration_ms=duration_ms,
        )

    async def capture_pane(self) -> str:
        await self.start()
        output = await self._run_tmux(
            ["capture-pane", "-p", "-S", f"-{self._history_limit}", "-t", self._session_name],
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

    async def _poll_until_marker(self, marker: str, timeout: float, start: float) -> str:
        interval = 0.2
        while time.monotonic() - start < timeout:
            pane = await self.capture_pane()
            if marker in pane:
                return pane.split(marker)[0]
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
