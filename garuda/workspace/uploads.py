"""Host-side staging lifecycle for Docker CLI text uploads."""

import asyncio
import tempfile
from pathlib import Path

from garuda.workspace.settlement import settle_task


async def upload_text(docker_base: list[str], container: str, target: str, content: str) -> None:
    payload = content.encode("utf-8")
    with tempfile.TemporaryDirectory(prefix="garuda-upload-") as directory:
        temp = Path(directory) / "content.txt"
        staging = asyncio.create_task(asyncio.to_thread(temp.write_bytes, payload))
        try:
            await asyncio.shield(staging)
        except asyncio.CancelledError as cancelled:
            # Don't delete the directory while a thread can still create a file.
            try:
                await settle_task(staging)
            except Exception as exc:
                raise cancelled from exc
            raise
        process = await asyncio.create_subprocess_exec(
            *docker_base, "cp", str(temp), f"{container}:{target}",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await process.communicate()
        if process.returncode != 0:
            raise RuntimeError(stderr.decode(errors="replace").strip())
