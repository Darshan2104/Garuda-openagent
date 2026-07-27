"""Environment liveness: tell a dead sandbox apart from a failing command.

The ``Environment`` protocol only reports ``ExecResult(exit_code=...)``, so a
container that has gone away looks exactly like a command that exited non-zero.
An agent cannot recover from the first and must recover from the second, so
conflating them is expensive: it keeps reasoning, editing and finally declaring
success against a box that no longer exists.

``HealthMonitoredEnvironment`` wraps any Environment and raises
``EnvironmentUnavailableError`` the moment a transport-level failure is
recognised. Wrapping (rather than editing each backend) means local, docker,
remote and any embedder-supplied adapter all gain the behaviour, and any tool
that touches the environment is covered without knowing this module exists.
"""

import logging
import re
from typing import Any

from garuda.types import ExecResult

logger = logging.getLogger(__name__)


class EnvironmentUnavailableError(RuntimeError):
    """The workspace itself is gone; no further tool call can succeed.

    Distinct from an ordinary tool error: the loop must abort the run rather
    than steer the model toward a different approach.
    """

    def __init__(self, reason: str, detail: str = ""):
        self.reason = reason
        self.detail = detail
        super().__init__(f"Environment unavailable: {reason}" + (f" ({detail})" if detail else ""))


# Transport-level failures. These are emitted by the runtime *around* the
# command, never by a command the agent meant to run, so matching them in
# stdout/stderr is safe. Kept deliberately narrow: a false positive aborts a
# live run, which is worse than the zombie run we are preventing.
FATAL_OUTPUT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("service_not_running", re.compile(r'service "[^"]+" is not running', re.I)),
    ("no_such_container", re.compile(r"\bNo such container\b", re.I)),
    ("container_not_running", re.compile(r"\bcontainer .{0,64}? is not running\b", re.I)),
    ("daemon_unreachable", re.compile(r"Cannot connect to the Docker daemon", re.I)),
    ("daemon_unreachable", re.compile(r"error during connect: .*docker", re.I)),
    ("sandbox_gone", re.compile(r"\bsandbox (?:not found|has been (?:deleted|destroyed))\b", re.I)),
    ("workspace_gone", re.compile(r"\bworkspace .{0,64}? (?:not found|no longer exists)\b", re.I)),
)

# Exception *type names* raised by a backend when the remote side has gone away.
# Matched by name so this module needs no dependency on any SDK.
FATAL_EXCEPTION_NAMES = frozenset(
    {
        "ContainerNotFoundError",
        "SandboxNotFoundError",
        "WorkspaceNotFoundError",
        "NotFoundError",
        "ConnectionResetError",
        "ConnectionRefusedError",
    }
)

# Backstop for a runtime whose failure text we do not recognise: this many
# consecutive environment operations raising the same exception type means the
# transport is broken, not that the agent is unlucky.
CONSECUTIVE_FAILURE_LIMIT = 4


def classify_exec(result: ExecResult) -> str | None:
    """Return a fatal-reason tag for an ExecResult, or None if it is a normal result.

    Only non-zero exits are inspected: a command that *succeeded* did so against
    a live environment, whatever its output happens to contain. That also keeps
    an agent grepping logs for the literal string "No such container" from
    tripping the detector.
    """
    if result.exit_code == 0:
        return None
    blob = f"{result.stdout or ''}\n{result.stderr or ''}"
    if not blob.strip():
        return None
    for reason, pattern in FATAL_OUTPUT_PATTERNS:
        if pattern.search(blob):
            return reason
    return None


def classify_exception(exc: BaseException) -> str | None:
    """Return a fatal-reason tag for an exception raised by an environment call."""
    name = type(exc).__name__
    if name in FATAL_EXCEPTION_NAMES:
        return f"exception:{name}"
    for reason, pattern in FATAL_OUTPUT_PATTERNS:
        if pattern.search(str(exc)):
            return reason
    return None


class HealthMonitoredEnvironment:
    """Environment proxy that fails loudly once the workspace is unreachable.

    Unknown attributes are forwarded, so backend extras (``persistent_execute``,
    ``upload_file``, ``task_env_config``) keep working. Once a fatal condition is
    seen the proxy latches: every later call raises immediately instead of
    waiting on a transport that will not answer.
    """

    def __init__(self, env: Any):
        self._env = env
        self._dead_reason: str | None = None
        self._dead_detail: str = ""
        self._consecutive_failures = 0
        self._last_exception_name: str | None = None

    # -- introspection -----------------------------------------------------

    @property
    def inner(self) -> Any:
        return self._env

    @property
    def is_alive(self) -> bool:
        return self._dead_reason is None

    @property
    def dead_reason(self) -> str | None:
        return self._dead_reason

    def health(self) -> dict[str, Any]:
        return {
            "alive": self.is_alive,
            "reason": self._dead_reason,
            "detail": self._dead_detail,
            "consecutive_failures": self._consecutive_failures,
        }

    # -- internals ---------------------------------------------------------

    def _die(self, reason: str, detail: str = "") -> None:
        if self._dead_reason is None:
            self._dead_reason = reason
            self._dead_detail = detail[:500]
            logger.error("Environment declared unavailable: %s (%s)", reason, self._dead_detail)
        raise EnvironmentUnavailableError(self._dead_reason, self._dead_detail)

    def _check_latched(self) -> None:
        if self._dead_reason is not None:
            raise EnvironmentUnavailableError(self._dead_reason, self._dead_detail)

    def _note_exception(self, exc: BaseException) -> None:
        """Track repeats of an unrecognised transport error as a fallback signal."""
        name = type(exc).__name__
        if name == self._last_exception_name:
            self._consecutive_failures += 1
        else:
            self._last_exception_name = name
            self._consecutive_failures = 1
        if self._consecutive_failures >= CONSECUTIVE_FAILURE_LIMIT:
            self._die(
                "repeated_environment_errors",
                f"{self._consecutive_failures} consecutive {name} from the environment",
            )

    # -- Environment protocol ---------------------------------------------

    async def execute(
        self,
        command: str,
        timeout: float | None = None,
        cwd: str | None = None,
    ) -> ExecResult:
        self._check_latched()
        try:
            result = await self._env.execute(command, timeout=timeout, cwd=cwd)
        except EnvironmentUnavailableError:
            raise
        except Exception as exc:
            reason = classify_exception(exc)
            if reason:
                self._die(reason, str(exc))
            self._note_exception(exc)
            raise
        reason = classify_exec(result)
        if reason:
            self._die(reason, f"{result.stdout or ''} {result.stderr or ''}".strip())
        self._consecutive_failures = 0
        self._last_exception_name = None
        return result

    async def read_file(self, path: str) -> str:
        self._check_latched()
        try:
            return await self._env.read_file(path)
        except EnvironmentUnavailableError:
            raise
        except Exception as exc:
            reason = classify_exception(exc)
            if reason:
                self._die(reason, str(exc))
            # A missing file is an ordinary outcome, not a transport failure, so
            # it must not feed the consecutive-failure backstop.
            if not isinstance(exc, (FileNotFoundError, IsADirectoryError, PermissionError)):
                self._note_exception(exc)
            raise

    async def write_file(self, path: str, content: str) -> None:
        self._check_latched()
        try:
            await self._env.write_file(path, content)
        except EnvironmentUnavailableError:
            raise
        except Exception as exc:
            reason = classify_exception(exc)
            if reason:
                self._die(reason, str(exc))
            self._note_exception(exc)
            raise

    @property
    def workspace_root(self) -> str:
        return self._env.workspace_root

    def __getattr__(self, name: str) -> Any:
        # Only called for attributes this proxy does not define, so backend
        # extras stay reachable. Guarded against the wrapped env being absent
        # during unpickling/partial construction.
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(self._env, name)


def monitored(env: Any) -> Any:
    """Wrap ``env`` for health monitoring, idempotently."""
    if isinstance(env, HealthMonitoredEnvironment):
        return env
    return HealthMonitoredEnvironment(env)
