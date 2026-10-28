from garuda.tools.protocol import ToolContext, ToolEffect
from garuda.types import ToolResult
from garuda.workspace.protocol import Environment


class InvokeSubagentTool:
    effect = ToolEffect.EXTERNAL_SIDE_EFFECT
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

    @classmethod
    def limited_to(cls, names: list[str]) -> "InvokeSubagentTool":
        """A copy whose schema enumerates exactly ``names`` (H.6)."""
        import copy

        tool = cls()
        tool.parameters = copy.deepcopy(cls.parameters)
        tool.parameters["properties"]["profile"] = {
            "type": "string", "enum": list(names),
            "description": f"The subagent to start: one of {', '.join(names) or '(none)'}",
        }
        return tool

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
        from garuda.core.child_result import (
            build_child_result_envelope,
            render_child_result_handoff,
        )

        budget_source = getattr(ctx.subagent_runner, "parent_output_budget", None)
        output_budget = budget_source() if callable(budget_source) else None
        envelope = build_child_result_envelope(
            result,
            profile,
            storage=getattr(ctx.subagent_runner, "parent_buffer", None),
            parent_tools=getattr(ctx.subagent_runner, "parent_tools", None),
            output_budget=output_budget,
            workspace=result.metadata.get("workspace_evidence"),
        )
        metadata = (
            {"child_result": envelope}
            if envelope.get("outcome", {}).get("validation") != "not_declared"
            else {}
        )
        return ToolResult(
            tool_call_id="",
            content=render_child_result_handoff(summary, envelope, output_budget),
            is_error=not result.success,
            metadata=metadata,
        )
