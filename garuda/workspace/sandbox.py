"""OS-level sandbox: hardened bubblewrap (Linux) or Seatbelt (macOS).

Unlike a naive wrapper, this environment:
* fails loudly when no backend is available (unless ``require_sandbox=False``),
  instead of silently running unconfined;
* scrubs the subprocess environment down to a passthrough allowlist so secrets
  are never exposed to sandboxed commands;
* blocks network egress by default (``--unshare-net`` / ``deny network*``).
"""

import os
from pathlib import Path

from garuda.types import ExecResult
from garuda.workspace.local import LocalEnvironment
from garuda.workspace.sandbox_policy import (
    SandboxPolicy,
    SandboxUnavailableError,
    build_bwrap_command,
    build_clean_env,
    build_seatbelt_command,
    build_seatbelt_profile,
    describe_unavailable,
    detect_sandbox_backend,
    to_shell_string,
)


class SandboxEnvironment:
    """Restrict command execution with an OS sandbox backend."""

    def __init__(
        self,
        workspace_root: str | Path | None = None,
        policy: SandboxPolicy | None = None,
    ):
        self._inner = LocalEnvironment(workspace_root=workspace_root)
        self._workspace_root = self._inner.workspace_root
        self._policy = policy or SandboxPolicy()
        self._backend = detect_sandbox_backend()
        if self._backend is None and self._policy.require_sandbox:
            raise SandboxUnavailableError(describe_unavailable())

    @property
    def workspace_root(self) -> str:
        return self._workspace_root

    @property
    def backend(self) -> str | None:
        return self._backend

    def is_sandboxed(self) -> bool:
        return self._backend is not None

    def _clean_env(self) -> dict[str, str]:
        return build_clean_env(self._policy, dict(os.environ))

    def _wrap(self, command: str, cwd: str | None) -> tuple[str, dict[str, str] | None]:
        """Return (shell_command, subprocess_env) for the chosen backend.

        The returned env is the scrubbed env for the launcher process; bwrap
        additionally re-applies it inside the namespace via ``--setenv``.
        """
        workdir = cwd or self._workspace_root
        clean_env = self._clean_env()
        if self._backend == "bwrap":
            from shutil import which

            argv = build_bwrap_command(command, workdir, which("bwrap"), self._policy, clean_env)
            return to_shell_string(argv), clean_env
        if self._backend == "seatbelt":
            from shutil import which

            profile = build_seatbelt_profile(workdir, self._policy)
            argv = build_seatbelt_command(command, workdir, which("sandbox-exec"), profile)
            return to_shell_string(argv), clean_env
        # No backend and require_sandbox=False: run unconfined but still scrub env.
        return command, clean_env

    async def execute(
        self,
        command: str,
        timeout: float | None = 120.0,
        cwd: str | None = None,
    ) -> ExecResult:
        wrapped, env = self._wrap(command, cwd)
        return await self._inner.execute(
            wrapped, timeout=timeout, cwd=self._workspace_root, env=env
        )

    async def read_file(self, path: str) -> str:
        return await self._inner.read_file(path)

    async def write_file(self, path: str, content: str) -> None:
        return await self._inner.write_file(path, content)
