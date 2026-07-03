from garuda.tools.protocol import ToolContext
from garuda.types import ToolResult
from garuda.workspace.protocol import Environment


class TaskCompleteTool:
    name = "task_complete"
    description = (
        "Signal that the task is finished. Provide a summary and optional shell commands "
        "to verify the result. This triggers completion verification."
    )
    parameters = {
        "type": "object",
        "properties": {
            "summary": {"type": "string", "description": "What was accomplished and how it was verified"},
            "verification_commands": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional commands to run before accepting completion",
            },
            "answer_rationale": {
                "type": "string",
                "description": (
                    "If you considered more than one candidate answer/approach, state which you "
                    "chose and why the alternatives were rejected. Helps verification confirm the "
                    "final answer is unambiguous."
                ),
            },
        },
        "required": ["summary"],
    }

    async def execute(
        self,
        arguments: dict,
        env: Environment,
        ctx: ToolContext,
    ) -> ToolResult:
        return ToolResult(
            tool_call_id="",
            content="task_complete received — pending verification",
            metadata={"pending_verification": True, **arguments},
        )
