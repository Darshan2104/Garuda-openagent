"""``remember``: propose a note for the user to review (plan task H.9, #171)."""

from garuda.context.notes import MAX_TEXT, NotesRefused
from garuda.tools.protocol import ToolContext, ToolEffect
from garuda.types import ToolResult
from garuda.workspace.protocol import Environment


class RememberTool:
    """Records a *proposal*. Nothing is remembered until the user accepts it with
    ``garuda memory review``; this tool changes no memory file."""

    effect = ToolEffect.EXTERNAL_SIDE_EFFECT
    name = "remember"
    description = (
        "Propose a short note worth remembering in future runs (a convention, a preference, a "
        f"non-obvious fact). At most {MAX_TEXT} characters, no secrets. This only records a "
        "proposal: the user decides later whether it is kept."
    )
    parameters = {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": f"The note, one or two sentences "
                                                      f"(at most {MAX_TEXT} characters)"},
            "scope": {"type": "string", "enum": ["user", "project"],
                      "description": "user: applies to everything this user does; "
                                     "project: only to this repository"},
        },
        "required": ["text", "scope"],
    }

    async def execute(self, arguments: dict, env: Environment, ctx: ToolContext) -> ToolResult:
        ledger = getattr(ctx, "notes", None)
        if ledger is None:
            return ToolResult(tool_call_id="", is_error=True,
                              content="agent.notes_unavailable: proposing notes is not enabled")
        try:
            proposal = ledger.propose(str(arguments.get("text", "")),
                                      str(arguments.get("scope", "")), ctx.session_id)
        except NotesRefused as exc:
            return ToolResult(tool_call_id="", is_error=True, content=str(exc))
        return ToolResult(tool_call_id="", content=(
            f"Proposal {proposal.id[:8]} recorded for the user's review. It is not memory yet."))
