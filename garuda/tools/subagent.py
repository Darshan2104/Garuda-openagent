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
            "handoff": {
                "type": "string",
                "enum": ["none", "brief", "full"],
                "description": (
                    "How much of your context the subagent starts with. 'brief' "
                    "(recommended) passes a compact state card — your goal, files "
                    "changed, checks run, and retrievable buffer ids — which is what "
                    "a delegated subtask normally needs. 'none' starts it cold. "
                    "'full' copies the entire conversation and is expensive; use it "
                    "only when the subagent must reason about how the conversation "
                    "reached this point."
                ),
                "default": "brief",
            },
            "fork_context": {
                "type": "boolean",
                "description": (
                    "Deprecated alias for handoff='full'. Prefer handoff."
                ),
                "default": False,
            },
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
        # fork_context=True still means the whole transcript, so an existing caller
        # gets exactly what it asked for. It only wins when explicitly set: otherwise
        # its default False would silently override a handoff the model chose.
        handoff = arguments.get("handoff")
        if arguments.get("fork_context"):
            handoff = "full"
        elif handoff is None:
            handoff = "brief"
        result = await ctx.subagent_runner.run(profile, task, fork_parent_context=handoff)
        summary = format_subagent_summary(profile, result)
        return ToolResult(
            tool_call_id="",
            content=summary,
            is_error=not result.success,
        )
