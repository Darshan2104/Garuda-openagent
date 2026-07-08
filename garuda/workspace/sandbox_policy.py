"""OS-sandbox policy: backend detection, env scrubbing, and profile builders.

Two backends are supported:

* **bubblewrap** (`bwrap`) on Linux — namespace isolation with ``--clearenv``,
  ``--unshare-net`` for network egress control, and targeted read binds instead
  of exposing the whole host filesystem.
* **Seatbelt** (`sandbox-exec`) on macOS — an SBPL profile that confines writes
  to the workspace and denies network unless explicitly allowed.

The pure builder functions here are unit-testable without either binary present.
"""

import platform
import shlex
import shutil
from dataclasses import dataclass, field

# Environment variables that are safe to expose to sandboxed commands. Anything
# not on this list (API keys, tokens, cloud creds, SSH agent sockets, …) is
# stripped so a sandboxed command cannot read the harness's secrets.
DEFAULT_ENV_PASSTHROUGH = (
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "SHELL",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TERM",
    "TZ",
    "TMPDIR",
    "PWD",
)

# Read-only host paths a normal command needs to run (interpreters, libs, certs).
DEFAULT_BWRAP_RO_PATHS = (
    "/usr",
    "/bin",
    "/sbin",
    "/lib",
    "/lib64",
    "/etc",
    "/opt",
)

# Writable paths beyond the workspace that most tooling expects.
DEFAULT_WRITABLE_PATHS = (
    "/dev",
    "/private/tmp",
    "/private/var/folders",
    "/tmp",
)


class SandboxUnavailableError(RuntimeError):
    """Raised when a sandbox is required but no backend is available."""


@dataclass
class DockerLimits:
    """Resource and network limits applied to docker/remote containers.

    ``network="none"`` disables egress entirely; ``"bridge"`` (default) keeps
    normal networking. ``no_new_privileges`` blocks setuid escalation inside
    the container.
    """

    memory: str | None = "2g"
    cpus: str | None = "2"
    pids_limit: int | None = 512
    network: str = "bridge"
    no_new_privileges: bool = True

    def to_run_args(self) -> list[str]:
        args: list[str] = []
        if self.memory:
            args += ["--memory", self.memory]
        if self.cpus:
            args += ["--cpus", self.cpus]
        if self.pids_limit:
            args += ["--pids-limit", str(self.pids_limit)]
        if self.network:
            args += ["--network", self.network]
        if self.no_new_privileges:
            args += ["--security-opt", "no-new-privileges"]
        return args


@dataclass
class SandboxPolicy:
    """Declarative sandbox configuration.

    ``require_sandbox`` (default True) makes the environment fail loudly rather
    than silently run unconfined when no backend is present — pass
    ``--allow-unsandboxed`` / ``require_sandbox=False`` to opt out.
    """

    allow_network: bool = False
    require_sandbox: bool = True
    env_passthrough: tuple[str, ...] = DEFAULT_ENV_PASSTHROUGH
    extra_env: dict[str, str] = field(default_factory=dict)
    ro_paths: tuple[str, ...] = DEFAULT_BWRAP_RO_PATHS
    writable_paths: tuple[str, ...] = DEFAULT_WRITABLE_PATHS


def detect_sandbox_backend() -> str | None:
    """Return the available backend name (``"bwrap"``/``"seatbelt"``) or None."""
    system = platform.system()
    if system == "Linux" and shutil.which("bwrap"):
        return "bwrap"
    if system == "Darwin" and shutil.which("sandbox-exec"):
        return "seatbelt"
    return None


def build_clean_env(policy: SandboxPolicy, source_env: dict[str, str]) -> dict[str, str]:
    """Project ``source_env`` down to the policy's passthrough allowlist."""
    clean = {key: source_env[key] for key in policy.env_passthrough if key in source_env}
    clean.update(policy.extra_env)
    return clean


def _existing(paths: tuple[str, ...]) -> list[str]:
    import os

    return [p for p in paths if os.path.exists(p)]


def build_bwrap_command(
    command: str,
    workdir: str,
    bwrap_path: str,
    policy: SandboxPolicy,
    clean_env: dict[str, str],
) -> list[str]:
    """Build a hardened bubblewrap argv wrapping ``command``.

    Hardening vs. a naive ``--ro-bind / /``:
    * ``--clearenv`` + explicit ``--setenv`` — no secret env leakage.
    * ``--unshare-net`` unless ``allow_network`` — blocks egress.
    * targeted ``--ro-bind`` of only the paths that exist, not the whole root.
    """
    argv = [
        bwrap_path,
        "--die-with-parent",
        "--unshare-pid",
        "--unshare-uts",
        "--unshare-ipc",
        "--new-session",
        "--clearenv",
        "--proc", "/proc",
        "--dev", "/dev",
    ]
    if not policy.allow_network:
        argv.append("--unshare-net")
    for path in _existing(policy.ro_paths):
        argv += ["--ro-bind", path, path]
    argv += ["--bind", workdir, workdir]
    for path in _existing(policy.writable_paths):
        if path != "/dev":  # already provided via --dev
            argv += ["--bind", path, path]
    for key, value in clean_env.items():
        argv += ["--setenv", key, value]
    argv += ["--chdir", workdir, "/bin/bash", "-lc", command]
    return argv


def _sbpl_quote(path: str) -> str:
    """Escape a path for inclusion in an SBPL string literal."""
    return path.replace("\\", "\\\\").replace('"', '\\"')


def build_seatbelt_profile(workdir: str, policy: SandboxPolicy) -> str:
    """Build a macOS Seatbelt (SBPL) profile string.

    Denies everything by default, allows process exec and full read, confines
    writes to the workspace plus a small set of runtime paths, and denies
    network egress unless ``allow_network`` is set.

    File reads are intentionally NOT scoped down: Seatbelt has no working
    allow-then-deny-subpath override for ``file-read*`` (an unfiltered
    ``(allow file-read*)`` beats any later, more-specific deny — verified
    empirically), and a read allowlist tight enough to matter (denying e.g.
    ``~/.ssh``) breaks basic command execution because dyld needs broad library
    access, in a way that varies across macOS versions. So this backend confines
    writes and network only; see the module docstring in ``sandbox.py`` for the
    resulting on-disk-secret-read gap.
    """
    writable = [workdir, *[p for p in policy.writable_paths]]
    subpaths = "\n  ".join(f'(subpath "{_sbpl_quote(p)}")' for p in writable)
    network_rule = "(allow network*)" if policy.allow_network else "(deny network*)"
    return (
        "(version 1)\n"
        "(deny default)\n"
        "(allow process*)\n"
        "(allow sysctl-read)\n"
        "(allow mach-lookup)\n"
        "(allow file-read*)\n"
        f"(allow file-write*\n  {subpaths})\n"
        f"{network_rule}\n"
    )


def build_seatbelt_command(
    command: str,
    workdir: str,
    sandbox_exec_path: str,
    profile: str,
) -> list[str]:
    """Build a ``sandbox-exec -p <profile> /bin/bash -lc <command>`` argv."""
    return [
        sandbox_exec_path,
        "-p",
        profile,
        "/bin/bash",
        "-lc",
        command,
    ]


def describe_unavailable() -> str:
    system = platform.system()
    if system == "Linux":
        hint = "install bubblewrap (e.g. `apt install bubblewrap`)"
    elif system == "Darwin":
        hint = "sandbox-exec should ship with macOS; ensure /usr/bin/sandbox-exec is present"
    else:
        hint = "no OS sandbox backend is supported on this platform"
    return (
        f"No OS sandbox backend available on {system}. To {hint}, "
        "or re-run with --workspace-kind local (unconfined) / --allow-unsandboxed "
        "to bypass the sandbox requirement."
    )


# Kept for callers that build a shell string rather than an argv (e.g. tmux).
def to_shell_string(argv: list[str]) -> str:
    return " ".join(shlex.quote(part) for part in argv)
