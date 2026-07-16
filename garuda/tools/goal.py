"""Goal orchestration (Feature B) — the `update_goal` tool.

A single, high-level objective the agent maintains for the task. Unlike a plain
message (which gets summarized away), the goal is re-pinned into context by the loop
after each compaction, so it anchors long-horizon work and survives summarization —
and the model doesn't burn tokens re-deriving what it was doing. Granular step
tracking still belongs in the `todo` tool; the goal is the north star above it.
"""

from garuda.tools.protocol import ToolContext
from garuda.types import ToolResult
from garuda.workspace.protocol import Environment


def render_goal(goal: str, plan: list[str] | None) -> str:
    """Render a goal (+ optional short plan) into a compact, token-cheap block."""
    out = goal.strip()
    steps = [str(s).strip() for s in (plan or []) if str(s).strip()]
    if steps:
        out += "\nPlan:\n" + "\n".join(f"{i}. {s}" for i, s in enumerate(steps, start=1))
    return out


class UpdateGoalTool:
    name = "update_goal"
    description = (
        "Set or update your current high-level goal for this task (with an optional short "
        "plan). The goal is kept in context and automatically re-surfaced after the "
        "conversation is compacted, so it anchors long tasks and survives summarization. "
        "Update it as your understanding evolves. Use the todo tool for granular step-by-step "
        "tracking; update_goal is the single north-star objective above it."
    )
    parameters = {
        "type": "object",
        "properties": {
            "goal": {
                "type": "string",
                "description": "The current high-level objective, stated concisely",
            },
            "plan": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional short ordered list of steps to reach the goal",
            },
        },
        "required": ["goal"],
    }

    def __init__(self) -> None:
        # Keyed by session_id (tool instances are shared across sessions in the
        # registry), holding the rendered goal block for re-pinning by the loop.
        self._sessions: dict[str, str] = {}

    def get_goal(self, session_id: str) -> str:
        return self._sessions.get(session_id, "")

    async def execute(
        self,
        arguments: dict,
        env: Environment,
        ctx: ToolContext,
    ) -> ToolResult:
        goal = (arguments.get("goal") or "").strip()
        if not goal:
            return ToolResult(
                tool_call_id="",
                content="goal must be a non-empty string describing your current objective.",
                is_error=True,
            )
        plan = arguments.get("plan")
        if plan is not None and not isinstance(plan, list):
            return ToolResult(
                tool_call_id="",
                content="plan must be an array of step strings.",
                is_error=True,
            )
        rendered = render_goal(goal, plan)
        self._sessions[ctx.session_id] = rendered
        return ToolResult(tool_call_id="", content=f"Goal updated.\n{rendered}")
