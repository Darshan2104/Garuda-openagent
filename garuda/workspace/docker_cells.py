"""Disposable Docker cell lifetime shared by local and remote Docker owners."""

import asyncio
from contextlib import asynccontextmanager
from uuid import uuid4

from garuda.workspace.cells import WORKER, CellError, cell_process, finish_cleanup


async def _control(argv: list[str]) -> tuple[int, bytes, bytes]:
    async with cell_process(argv) as process:
        stdout, stderr = await asyncio.wait_for(process.communicate(), 10)
        return process.returncode, stdout, stderr


@asynccontextmanager
async def docker_code_cell(docker: list[str], container_name: str):
    # Prove the target workspace still exists before opening its cell. No Python
    # or additional packages are required in the workspace's own image.
    status, stdout, _ = await _control([
        *docker, "inspect", "--format", "{{.State.Running}}", container_name,
    ])
    if status != 0 or stdout.strip() != b"true":
        raise CellError("Code cells unsupported: Docker workspace is not running")
    name = f"garuda-cell-{uuid4().hex}"
    argv = [
        *docker, "create", "--name", name, "--pull=never", "-i",
        "--network=none", "--read-only", "--cap-drop=ALL",
        "--security-opt=no-new-privileges:true", "--pids-limit=64",
        "--memory=128m", "--cpus=1", "--user=65534:65534",
        "--tmpfs=/tmp:rw,noexec,nosuid,size=16m", "--workdir=/tmp",
        "--entrypoint=python3", "python:3.12-slim", "-I", "-u", "-c", WORKER,
    ]

    async def remove():
        try:
            status, _, stderr = await _control([*docker, "rm", "-f", name])
        except (OSError, TimeoutError) as exc:
            raise CellError("Cell container cleanup unconfirmed; side effects unknown") from exc
        if status and b"No such container" not in stderr:
            raise CellError("Cell container cleanup unconfirmed; side effects unknown")

    try:
        # Create before attach: cancellation cannot leave a late docker-run
        # request starting executable code after cleanup found no container.
        status, _, stderr = await _control(argv)
        if status:
            raise CellError(
                "Code cell unavailable: preinstall python:3.12-slim on the workspace daemon; "
                + stderr.decode(errors="replace")[:1024]
            )
        async with cell_process([*docker, "start", "--attach", "-i", name]) as process:
            yield process
    finally:
        # Removing the PID namespace kills even detached descendants. Stopping
        # the docker client alone does not terminate container-side execution.
        await finish_cleanup(asyncio.create_task(remove()))
