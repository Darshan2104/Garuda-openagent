import asyncio
import shlex
import tempfile
import time
import uuid
from pathlib import Path

from garuda.types import ExecResult
from garuda.workspace.sandbox_policy import DockerLimits


class DockerWorkspace:
    def __init__(
        self,
        workspace_root: str | Path,
        image: str = "ubuntu:22.04",
        container_name: str | None = None,
        limits: "DockerLimits | None" = None,
    ):
        self._workspace_host = Path(workspace_root).resolve()
        self._image = image
        self._container_name = container_name or f"garuda-{uuid.uuid4().hex[:8]}"
        self._limits = limits or DockerLimits()
        self._container_id: str | None = None
        self._environment: DockerEnvironment | None = None

    @property
    def container_name(self) -> str:
        return self._container_name

    async def start(self) -> None:
        if self._container_id:
            return
        self._workspace_host.mkdir(parents=True, exist_ok=True)
        process = await asyncio.create_subprocess_exec(
            "docker",
            "run",
            "-d",
            "--name",
            self._container_name,
            "-v",
            f"{self._workspace_host}:/workspace",
            "-w",
            "/workspace",
            *self._limits.to_run_args(),
            self._image,
            "sleep",
            "infinity",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        if process.returncode != 0:
            raise RuntimeError(stderr.decode(errors="replace").strip())
        self._container_id = stdout.decode().strip()
        self._environment = DockerEnvironment(self._container_name, workspace_root="/workspace")

    async def stop(self) -> None:
        if not self._container_id:
            return
        process = await asyncio.create_subprocess_exec(
            "docker",
            "rm",
            "-f",
            self._container_name,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await process.communicate()
        self._container_id = None
        self._environment = None

    def get_environment(self) -> "DockerEnvironment":
        if self._environment is None:
            raise RuntimeError("DockerWorkspace not started")
        return self._environment


class DockerEnvironment:
    def __init__(self, container_name: str, workspace_root: str = "/workspace"):
        self._container_name = container_name
        self._workspace_root = workspace_root

    @property
    def workspace_root(self) -> str:
        return self._workspace_root

    async def execute(
        self,
        command: str,
        timeout: float | None = 120.0,
        cwd: str | None = None,
    ) -> ExecResult:
        workdir = cwd or self._workspace_root
        # Bound the command *inside the container* with coreutils `timeout`, so it is
        # killed container-side even if the local `docker exec` client is torn down
        # (docker does not kill the exec'd process when the client detaches). The
        # client-side wait_for is a slightly-longer backstop.
        inner = f"cd {shlex.quote(workdir)} && {command}"
        if timeout is not None:
            shell = f"timeout --kill-after=5s {int(timeout)}s bash -lc {shlex.quote(inner)}"
            client_timeout: float | None = timeout + 15
        else:
            shell = inner
            client_timeout = None
        start = time.monotonic()
        process = await asyncio.create_subprocess_exec(
            "docker",
            "exec",
            self._container_name,
            "bash",
            "-lc",
            shell,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                process.communicate(),
                timeout=client_timeout,
            )
        except (TimeoutError, asyncio.TimeoutError):
            process.kill()
            try:
                await process.wait()
            except ProcessLookupError:
                pass
            return ExecResult(
                stdout="",
                stderr=f"Command timed out after {timeout}s and was killed.",
                exit_code=124,
                duration_ms=int((time.monotonic() - start) * 1000),
                truncated=True,
            )
        duration_ms = int((time.monotonic() - start) * 1000)
        return ExecResult(
            stdout=stdout_bytes.decode(errors="replace"),
            stderr=stderr_bytes.decode(errors="replace"),
            exit_code=process.returncode or 0,
            duration_ms=duration_ms,
        )

    async def read_file(self, path: str) -> str:
        target = path if path.startswith("/") else f"{self._workspace_root}/{path}"
        result = await self.execute(f"cat {shlex.quote(target)}")
        if result.exit_code != 0:
            raise FileNotFoundError(result.stderr.strip() or f"Cannot read {path}")
        return result.stdout

    async def write_file(self, path: str, content: str) -> None:
        temp = Path(tempfile.gettempdir()) / f"garuda-upload-{uuid.uuid4().hex}.txt"
        await asyncio.to_thread(temp.write_text, content, encoding="utf-8")
        target = path if path.startswith("/") else f"{self._workspace_root}/{path}"
        process = await asyncio.create_subprocess_exec(
            "docker",
            "cp",
            str(temp),
            f"{self._container_name}:{target}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await process.communicate()
        temp.unlink(missing_ok=True)
        if process.returncode != 0:
            raise RuntimeError(stderr.decode(errors="replace").strip())
