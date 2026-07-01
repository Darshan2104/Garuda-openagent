from garuda.tools.protocol import ToolContext
from garuda.types import ToolResult
from garuda.workspace.protocol import Environment


class InvokeSubagentTool:
    name = "invoke_subagent"
    description = (
        "Delegate a subtask to a specialized subagent (e.g. explore, plan). "
        "Returns a distilled summary without polluting the main context."
    )
    parameters = {
        "type": "object",
        "properties": {
            "profile": {
                "type": "string",
                "description": "Subagent profile name (explore, plan, or custom)",
            },
            "task": {"type": "string", "description": "Task for the subagent"},
        },
        "required": ["profile", "task"],
    }

    async def execute(
        self,
        arguments: dict,
        env: Environment,
        ctx: ToolContext,
    ) -> ToolResult:
        if ctx.subagent_runner is None:
            return ToolResult(
                tool_call_id="",
                content="Subagent runner not configured",
                is_error=True,
            )
        from garuda.core.subagent import format_subagent_summary

        profile = arguments["profile"]
        task = arguments["task"]
        result = await ctx.subagent_runner.run(profile, task)
        summary = format_subagent_summary(profile, result)
        return ToolResult(
            tool_call_id="",
            content=summary,
            is_error=not result.success,
        )
