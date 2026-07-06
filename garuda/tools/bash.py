from garuda.tools.protocol import ToolContext
from garuda.types import ToolResult
from garuda.workspace.protocol import Environment


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
        kwargs: dict = {}
        if timeout is not None:
            kwargs["timeout"] = float(timeout)
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
        return ToolResult(
            tool_call_id="",
            content=output,
            is_error=failed,
        )
