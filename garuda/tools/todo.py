from garuda.tools.protocol import ToolContext
from garuda.types import ToolResult
from garuda.workspace.protocol import Environment

_VALID_STATUSES = ("pending", "in_progress", "completed")
_STATUS_MARKS = {
    "pending": "☐",
    "in_progress": "▶",
    "completed": "☑",
}


class TodoTool:
    name = "todo"
    description = (
        "Maintain your task list for multi-step work. Each call replaces the entire list, "
        "so always send every item with its current status. Keep exactly one item "
        "in_progress at a time, mark items completed as soon as they are done, and "
        "update the list as you work."
    )
    parameters = {
        "type": "object",
        "properties": {
            "todos": {
                "type": "array",
                "description": "The complete task list (replaces any previous list)",
                "items": {
                    "type": "object",
                    "properties": {
                        "content": {"type": "string", "description": "Task description"},
                        "status": {
                            "type": "string",
                            "enum": list(_VALID_STATUSES),
                            "description": "Task status",
                        },
                    },
                    "required": ["content", "status"],
                },
            },
        },
        "required": ["todos"],
    }

    def __init__(self) -> None:
        # Keyed by session_id: the registry shares tool instances across
        # sessions, so per-session keying avoids cross-session bleed.
        self._sessions: dict[str, list[dict]] = {}

    def get_todos(self, session_id: str) -> list[dict]:
        return list(self._sessions.get(session_id, []))

    async def execute(
        self,
        arguments: dict,
        env: Environment,
        ctx: ToolContext,
    ) -> ToolResult:
        todos = arguments.get("todos")
        if not isinstance(todos, list):
            return ToolResult(
                tool_call_id="",
                content="todos must be an array of {content, status} objects.",
                is_error=True,
            )

        cleaned: list[dict] = []
        for i, item in enumerate(todos):
            if not isinstance(item, dict) or "content" not in item:
                return ToolResult(
                    tool_call_id="",
                    content=f"todos[{i}] must be an object with content and status.",
                    is_error=True,
                )
            status = item.get("status", "pending")
            if status not in _VALID_STATUSES:
                return ToolResult(
                    tool_call_id="",
                    content=(
                        f"todos[{i}] has invalid status {status!r}; "
                        f"must be one of {', '.join(_VALID_STATUSES)}."
                    ),
                    is_error=True,
                )
            cleaned.append({"content": str(item["content"]), "status": status})

        self._sessions[ctx.session_id] = cleaned

        if not cleaned:
            return ToolResult(tool_call_id="", content="Todo list cleared.")
        rendered = "\n".join(
            f"{_STATUS_MARKS[item['status']]} {item['content']}" for item in cleaned
        )
        return ToolResult(tool_call_id="", content=rendered)
