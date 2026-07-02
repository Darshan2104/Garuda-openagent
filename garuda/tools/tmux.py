from garuda.tools.protocol import ToolContext
from garuda.types import ToolResult
from garuda.workspace.protocol import Environment
from garuda.workspace.tmux import TmuxEnvironment


def _as_tmux(env: Environment) -> TmuxEnvironment:
    if isinstance(env, TmuxEnvironment):
        return env
    raise TypeError("tmux tools require TmuxEnvironment")


class TmuxExecTool:
    name = "tmux_exec"
    description = (
        "Run a command in the persistent tmux session. Uses marker-based polling "
        "to detect completion and report the real exit code. Set marker_polling "
        "to false only to send raw keys to an interactive program (TUI) already "
        "running in the pane; no completion marker is appended in that mode."
    )
    parameters = {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "Shell command to run in tmux"},
            "timeout": {"type": "number", "description": "Max seconds to wait", "default": 120},
            "marker_polling": {
                "type": "boolean",
                "description": (
                    "Poll for a completion marker and exit code (default). Set false "
                    "to send raw keys to an interactive program without a marker."
                ),
                "default": True,
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
        tmux = _as_tmux(env)
        result = await tmux.send_command(
            arguments["command"],
            timeout=float(arguments.get("timeout", 120)),
            marker_polling=bool(arguments.get("marker_polling", True)),
        )
        output = result.stdout or "Command ran successfully with no output."
        if result.stderr:
            output = f"{output}\n{result.stderr}" if result.stdout else result.stderr
        return ToolResult(tool_call_id="", content=output, is_error=result.exit_code != 0)


class TmuxCaptureTool:
    name = "tmux_capture"
    description = "Capture the current tmux pane output."
    parameters = {"type": "object", "properties": {}, "required": []}

    async def execute(
        self,
        arguments: dict,
        env: Environment,
        ctx: ToolContext,
    ) -> ToolResult:
        tmux = _as_tmux(env)
        pane = await tmux.capture_pane()
        return ToolResult(tool_call_id="", content=pane)
