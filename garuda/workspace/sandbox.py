"""OS-level sandbox: hardened bubblewrap (Linux) or Seatbelt (macOS).

Unlike a naive wrapper, this environment:
* fails loudly when no backend is available (unless ``require_sandbox=False``),
  instead of silently running unconfined;
* scrubs the subprocess *environment* down to a passthrough allowlist, so a
  sandboxed command's env never contains the harness's API keys/tokens;
* blocks network egress by default (``--unshare-net`` / ``deny network*``).

**Known gap (macOS/Seatbelt only).** Env scrubbing and network denial are real
guarantees on both backends. File-*read* confinement is not: bwrap only ro-binds
an explicit allowlist (``ro_paths``), but Seatbelt's SBPL has no working
allow-then-deny-subpath override for ``file-read*`` (verified empirically — an
unfiltered ``(allow file-read*)`` beats any later/more-specific deny), and a
read-only allowlist tight enough to deny arbitrary host paths breaks basic exec
(dyld needs broad library access) in a way that's fragile across macOS versions.
So on macOS, a sandboxed command *can* read on-disk secrets outside the
workspace (e.g. ``~/.ssh``) even though it can't exfiltrate them over the
network (denied by default) or via env (scrubbed). See ``build_seatbelt_profile``.
"""

import logging
import os
from pathlib import Path

from garuda.types import ExecResult
from garuda.workspace.local import LocalEnvironment

logger = logging.getLogger(__name__)
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

    def _confine_cwd(self, cwd: str | None) -> str:
        """Clamp a model-supplied cwd to inside the workspace.

        The workdir becomes a writable bind (bwrap) / write subpath (Seatbelt), so an
        unconfined cwd like /Users/x/.ssh would defeat write confinement — the whole
        point of the sandbox. A cwd escaping the workspace is ignored.
        """
        if not cwd:
            return self._workspace_root
        root = Path(self._workspace_root).resolve()
        candidate = Path(cwd)
        resolved = (candidate if candidate.is_absolute() else root / candidate).resolve()
        try:
            resolved.relative_to(root)
        except ValueError:
            logger.warning("Ignoring sandbox cwd %r: outside the workspace", cwd)
            return self._workspace_root
        return str(resolved)

    def _wrap(self, command: str, cwd: str | None) -> tuple[str, dict[str, str] | None]:
        """Return (shell_command, subprocess_env) for the chosen backend.

        The returned env is the scrubbed env for the launcher process; bwrap
        additionally re-applies it inside the namespace via ``--setenv``.
        """
        workdir = self._confine_cwd(cwd)
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

    def _confine_real_path(self, path: str) -> None:
        """Re-check confinement against the *resolved* (symlink-following) path.

        ``execute()`` is confined at the syscall level regardless of symlinks — a
        mount namespace (bwrap) or Seatbelt subpath rule only exposes the real
        target, not whatever the workspace-relative name lexically looks like.
        ``read_file``/``write_file`` don't route through the OS sandbox at all, so
        an in-workspace symlink pointing outside the workspace (e.g. ``docs ->
        ~/.ssh``) would otherwise bypass the confinement ``--workspace-kind
        sandbox`` promises. Resolve and re-check here, specifically for this
        backend — unsandboxed ``LocalEnvironment`` keeps its existing lexical-only
        behavior, matching ``bash`` there (which is equally unconfined).
        """
        root = Path(self._workspace_root).resolve()
        candidate = Path(path)
        full = candidate if candidate.is_absolute() else root / candidate
        resolved = full.resolve()
        if resolved != root and not resolved.is_relative_to(root):
            raise PermissionError(
                f"Path {path!r} resolves outside the sandboxed workspace root {root} "
                "(possibly via a symlink); refusing."
            )

    async def read_file(self, path: str) -> str:
        self._confine_real_path(path)
        return await self._inner.read_file(path)

    async def write_file(self, path: str, content: str) -> None:
        self._confine_real_path(path)
        return await self._inner.write_file(path, content)
