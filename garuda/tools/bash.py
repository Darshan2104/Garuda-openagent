import time

from garuda.tools.protocol import ToolContext
from garuda.types import ToolResult
from garuda.workspace.protocol import Environment

# Applied when the caller sets no timeout and there is no wall-clock budget to
# derive one from. Mirrors the environment-layer default.
DEFAULT_TIMEOUT_SEC = 120.0

# Never squeeze a command below this, however little budget is left: a one-second
# ceiling turns every command into a failure and teaches the model nothing.
MIN_COMMAND_TIMEOUT_SEC = 15.0

BUDGET_CAPPED_NOTE = (
    "\n\n[budget] This command was capped at {capped:.0f}s (you asked for {asked:.0f}s) "
    "because only {remaining:.0f}s of the run's wall-clock budget remain. A single command "
    "may not consume the rest of the run."
)


def resolve_timeout(
    requested: float | None,
    deadline_monotonic: float | None,
    fraction: float,
) -> tuple[float | None, float | None, float | None]:
    """Bound a requested timeout by the share of remaining budget a command may use.

    Returns ``(effective, requested, remaining)`` where ``effective`` is what to
    run with and the other two are non-None only when a cap was actually applied,
    so the caller can explain the change to the model.
    """
    if deadline_monotonic is None:
        return (requested if requested is not None else DEFAULT_TIMEOUT_SEC), None, None
    remaining = deadline_monotonic - time.monotonic()
    if remaining <= 0:
        # Budget already gone: still allow a short command so the agent can write
        # out a final answer rather than being unable to act at all.
        return MIN_COMMAND_TIMEOUT_SEC, requested, 0.0
    ceiling = max(MIN_COMMAND_TIMEOUT_SEC, remaining * fraction)
    asked = requested if requested is not None else DEFAULT_TIMEOUT_SEC
    if asked <= ceiling:
        return asked, None, None
    return ceiling, asked, remaining


class BashTool:
    name = "bash"
    description = (
        "Execute a shell command in the workspace and return stdout, stderr, and exit code. "
        "Set timeout for long builds/tests (default 120s) and cwd to run in a subdirectory."
    )
    parameters = {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "Shell command to execute"},
            "timeout": {
                "type": "number",
                "description": "Max seconds to wait before the command is killed (default 120)",
            },
            "cwd": {
                "type": "string",
                "description": "Working directory to run the command in (default: workspace root)",
            },
        },
        "required": ["command"],
    }

    async def execute(
        self,
        arguments: dict,
        env: Environment,
        ctx: ToolContext,
    ) -> ToolResult:
        command = arguments["command"]
        timeout = arguments.get("timeout")
        cwd = arguments.get("cwd")
        effective, asked, remaining = resolve_timeout(
            float(timeout) if timeout is not None else None,
            getattr(ctx, "deadline_monotonic", None),
            getattr(ctx, "command_budget_fraction", 0.5),
        )
        kwargs: dict = {"timeout": effective}
        if cwd:
            kwargs["cwd"] = cwd
        # Persistent mode (opt-in, local env): reuse a long-lived shell so cwd/env
        # persist across calls. Falls back to per-call execution otherwise.
        if getattr(ctx, "persistent_shell", False) and hasattr(env, "persistent_execute"):
            result = await env.persistent_execute(command, **kwargs)
        else:
            result = await env.execute(command, **kwargs)

        failed = result.exit_code != 0
        if not result.stdout and not result.stderr:
            status = f"Command failed with no output (exit {result.exit_code})." if failed \
                else "Command ran successfully with no output."
            output = f"exit_code: {result.exit_code}\n{status}"
        else:
            output = (
                f"exit_code: {result.exit_code}\n"
                f"stdout:\n{result.stdout}\n"
                f"stderr:\n{result.stderr}"
            ).strip()
        if asked is not None:
            output += BUDGET_CAPPED_NOTE.format(
                capped=effective, asked=asked, remaining=remaining or 0.0
            )
        return ToolResult(
            tool_call_id="",
            content=output,
            is_error=failed,
        )
