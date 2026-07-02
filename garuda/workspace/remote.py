"""Remote Docker workspace using a configurable Docker daemon host."""

import asyncio
import os
import shlex
import tempfile
import time
import uuid
from pathlib import Path

from garuda.types import ExecResult
from garuda.workspace.sandbox_policy import DockerLimits


class RemoteWorkspace:
    """Spawn and manage a container on a remote Docker daemon (``DOCKER_HOST``)."""

    def __init__(
        self,
        workspace_root: str | Path,
        image: str = "ubuntu:22.04",
        container_name: str | None = None,
        docker_host: str | None = None,
        limits: "DockerLimits | None" = None,
    ):
        self._workspace_host = Path(workspace_root).resolve()
        self._image = image
        self._container_name = container_name or f"garuda-remote-{uuid.uuid4().hex[:8]}"
        self._docker_host = docker_host or os.environ.get("DOCKER_HOST")
        self._limits = limits or DockerLimits()
        self._container_id: str | None = None
        self._environment: RemoteEnvironment | None = None

    def _docker_base(self) -> list[str]:
        cmd = ["docker"]
        if self._docker_host:
            cmd.extend(["-H", self._docker_host])
        return cmd

    @property
    def container_name(self) -> str:
        return self._container_name

    @property
    def docker_host(self) -> str | None:
        return self._docker_host

    async def start(self) -> None:
        if self._container_id:
            return
        self._workspace_host.mkdir(parents=True, exist_ok=True)
        process = await asyncio.create_subprocess_exec(
            *self._docker_base(),
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
        self._environment = RemoteEnvironment(
            self._docker_base(),
            self._container_name,
            workspace_root="/workspace",
        )

    async def stop(self) -> None:
        if not self._container_id:
            return
        process = await asyncio.create_subprocess_exec(
            *self._docker_base(),
            "rm",
            "-f",
            self._container_name,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await process.communicate()
        self._container_id = None
        self._environment = None

    def get_environment(self) -> "RemoteEnvironment":
        if self._environment is None:
            raise RuntimeError("RemoteWorkspace not started")
        return self._environment


class RemoteEnvironment:
    def __init__(self, docker_base: list[str], container_name: str, workspace_root: str = "/workspace"):
        self._docker_base = docker_base
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
        shell = f"cd {shlex.quote(workdir)} && {command}"
        start = time.monotonic()
        process = await asyncio.create_subprocess_exec(
            *self._docker_base,
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
                timeout=timeout,
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
        temp = Path(tempfile.gettempdir()) / f"garuda-remote-upload-{uuid.uuid4().hex}.txt"
        await asyncio.to_thread(temp.write_text, content, encoding="utf-8")
        target = path if path.startswith("/") else f"{self._workspace_root}/{path}"
        process = await asyncio.create_subprocess_exec(
            *self._docker_base,
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
