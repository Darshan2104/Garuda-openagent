from garuda.tools.protocol import ToolContext
from garuda.types import ToolResult
from garuda.workspace.protocol import Environment


class TaskCompleteTool:
    name = "task_complete"
    description = (
        "Signal that the task is finished. Provide a summary and shell commands that would "
        "fail if the work were wrong. This triggers completion verification."
    )
    parameters = {
        "type": "object",
        "properties": {
            "summary": {
                "type": "string",
                "description": (
                    "What was accomplished, and for each thing the task asked for, the "
                    "observation that showed it was actually met — the output you saw, not "
                    "the intent you had."
                ),
            },
            "verification_commands": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Commands whose exit status would change if the work were wrong. Run what "
                    "you built and compare its real output against what the task specified. "
                    "Before listing a command, ask what it would do if the result were wrong; "
                    "if the answer is 'still exit 0', it is not verification. Showing that a "
                    "file exists, parses, imports, or prints proves nothing — those exit 0 for "
                    "work that is confidently incorrect. Check what was asked for, not merely "
                    "that what you built runs. If a requirement genuinely cannot be checked "
                    "from inside this environment, say so in the summary rather than "
                    "substituting a command that cannot fail."
                ),
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
