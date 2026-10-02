"""Read-only ACP roles run in Docker (plan task C.8a, #158).

An external ACP role whose permissions are ``readonly`` — a standalone run,
a sequential scout, planner or reviewer, or a fallback — runs only in
Docker-class confinement, and only when a preflight proves it:

- the source is mounted read-only (a write into it fails, and into its
  ``.git`` too);
- scratch space is writable and bounded (``/scratch``, ``/tmp``);
- the container user is not root, has no capabilities and cannot gain any;
- no Docker socket, host home or other workspace is mounted, and nothing
  from a host credential store (extra mounts under a hidden directory of your
  home, or the socket, are refused);
- the image holds a usable runtime you authorized in it — Garuda passes no
  host credential into the container.

Otherwise the run refuses with ``workspace.readonly_unenforced``. There is no
host substitution: a worktree or the no-edits guardrail is never offered in
its place. The harness's image (and any read-only tool mounts) come only from
your user ``garuda.yaml``:

.. code-block:: yaml

    harnesses:
      claude: {confinement: {image: my-claude-acp:latest}}
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

CONTAINER_WORKSPACE = "/workspace"
CODE = "workspace.readonly_unenforced"
_PROBE = (
    'test "$(id -u)" != 0'
    " && ! touch /workspace/.garuda-probe 2>/dev/null"
    " && { test ! -d /workspace/.git || ! touch /workspace/.git/.garuda-probe 2>/dev/null; }"
    " && touch /scratch/.garuda-probe && test ! -e /var/run/docker.sock"
)


class ConfinementRefused(Exception):
    def __init__(self, message: str):
        super().__init__(f"{CODE}: {message}")
        self.code = CODE


@dataclass(frozen=True)
class Confinement:
    image: str
    command: tuple[str, ...] = ()
    mounts: tuple[str, ...] = field(default_factory=tuple)

    @classmethod
    def from_config(cls, harness: dict | None) -> "Confinement | None":
        spec = (harness or {}).get("confinement")
        if not spec:
            return None
        return cls(image=spec["image"], command=tuple(spec.get("command", ())),
                   mounts=tuple(spec.get("mounts", ())))


def _check_mounts(mounts, workspace: Path) -> None:
    home = Path.home().resolve()
    for source in mounts:
        path = Path(source).resolve()
        rel = path.relative_to(home).parts if path.is_relative_to(home) else ()
        if rel and rel[0].startswith("."):
            raise ConfinementRefused(f"{source} is a hidden directory of your home "
                                     "(a possible credential store)")
        if path == home:
            raise ConfinementRefused("your home directory cannot be mounted")
        if "docker.sock" in path.name:
            raise ConfinementRefused("the Docker socket cannot be mounted")
        if path == workspace or workspace in path.parents or path in workspace.parents:
            raise ConfinementRefused(f"{source} overlaps the workspace")


def docker_argv(workspace, confinement: Confinement, command: list[str]) -> list[str]:
    docker = shutil.which("docker") or "docker"
    ws = str(Path(workspace).resolve())
    argv = [
        docker, "run", "-i", "--rm", "--read-only",
        "--user", "65534:65534", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
        "--tmpfs", "/tmp:rw,size=256m,mode=1777",
        "--tmpfs", "/scratch:rw,size=512m,uid=65534,gid=65534,mode=700",
        "-e", "HOME=/scratch", "-e", "TMPDIR=/scratch",
        "-v", f"{ws}:{CONTAINER_WORKSPACE}:ro", "-w", CONTAINER_WORKSPACE,
    ]
    for source in confinement.mounts:
        real = str(Path(source).resolve())
        argv += ["-v", f"{real}:{real}:ro"]
    return argv + [confinement.image, *command]


def preflight(workspace, confinement: Confinement | None, *, timeout: float = 120) -> None:
    """Prove the confinement holds, or refuse. Never substitutes the host."""
    if confinement is None:
        raise ConfinementRefused("this harness has no confinement image configured "
                                 "(harnesses.<id>.confinement.image in your garuda.yaml)")
    if shutil.which("docker") is None:
        raise ConfinementRefused("Docker is not available")
    root = Path(workspace).resolve()
    _check_mounts(confinement.mounts, root)
    argv = docker_argv(root, confinement, ["sh", "-c", _PROBE])
    argv.insert(2, "--pull")
    argv.insert(3, "never")
    argv.insert(4, "--network")
    argv.insert(5, "none")
    try:
        result = subprocess.run(argv, capture_output=True, timeout=timeout,
                                stdin=subprocess.DEVNULL, env=_docker_env())
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ConfinementRefused(f"the confinement probe did not run: {exc}") from exc
    if result.returncode != 0:
        raise ConfinementRefused(
            "the confinement probe failed (the image is missing, runs as root, or the "
            "source was writable): " + result.stderr.decode(errors="replace").strip()[-300:])
    for leftover in (root / ".garuda-probe", root / ".git" / ".garuda-probe"):
        if leftover.exists():
            raise ConfinementRefused(f"the probe wrote {leftover}; the source is not read-only")


def _docker_env() -> dict[str, str]:
    keep = ("PATH", "HOME", "DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG", "LANG")
    return {k: os.environ[k] for k in keep if k in os.environ}
