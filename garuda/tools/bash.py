from garuda.tools.protocol import ToolContext
from garuda.types import ToolResult
from garuda.workspace.protocol import Environment


class BashTool:
    name = "bash"
    description = "Execute a shell command in the workspace and return stdout, stderr, and exit code."
    parameters = {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "Shell command to execute"},
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
        result = await env.execute(command)
        output = (
            f"exit_code: {result.exit_code}\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        ).strip()
        if not result.stdout and not result.stderr:
            output = f"exit_code: {result.exit_code}\nCommand ran successfully with no output."
        return ToolResult(
            tool_call_id="",
            content=output,
            is_error=result.exit_code != 0,
        )
