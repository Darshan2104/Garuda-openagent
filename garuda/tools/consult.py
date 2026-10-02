"""``consult``: ask another role a bounded question (plan task G.2, #170).

Present only for a role the user granted consult targets (the enum is exactly those targets).
It is a request to the consult *service*, which authorizes again from the session's own
record, so a forged tool definition gains nothing: the grant is not the tool's presence.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from garuda.consult.errors import ConsultRefused
from garuda.consult.service import ConsultRequest
from garuda.tools.protocol import ToolContext, ToolEffect
from garuda.types import ToolResult
from garuda.workspace.protocol import Environment


@dataclass
class ConsultContext:
    """What the tool needs, fixed by the harness at run start (never by the model)."""

    service: Any
    asker_session: str
    root_session: str
    targets: tuple[str, ...]
    workspace: str
    quiesce: Any = None
    turn: Any = None  # callable returning the current turn


class ConsultTool:
    effect = ToolEffect.EXTERNAL_SIDE_EFFECT
    name = "consult"
    description = (
        "Ask another role one bounded question about your current work and get its answer back as "
        "labelled advice. It reads a read-only snapshot of your workspace; it cannot change "
        "anything, and its answer is advice, not verification."
    )
    parameters = {
        "type": "object",
        "properties": {
            "target": {"type": "string", "description": "The role to ask"},
            "question": {"type": "string", "description": "The question, specific and self-contained"},
            "brief": {"type": "string",
                      "description": "Optional background the role needs (kept short)"},
            "request_id": {"type": "string",
                           "description": "Optional idempotency key: repeating it returns the "
                                          "same answer without asking again"},
        },
        "required": ["target", "question"],
    }

    def limited_to(self, targets: tuple[str, ...]) -> ConsultTool:
        import copy

        tool = ConsultTool()
        tool.parameters = copy.deepcopy(self.parameters)
        tool.parameters["properties"]["target"] = {
            "type": "string", "enum": list(targets), "description": "The role to ask"}
        return tool

    async def execute(self, arguments: dict, env: Environment, ctx: ToolContext) -> ToolResult:
        context: ConsultContext | None = getattr(ctx, "consult", None)
        if context is None:
            return ToolResult(tool_call_id="", is_error=True,
                              content="consult.not_granted: this session may not consult")
        request = ConsultRequest(
            asker_session=context.asker_session, root_session=context.root_session,
            target=str(arguments.get("target", "")), question=str(arguments.get("question", "")),
            brief=str(arguments.get("brief") or ""),
            request_id=str(arguments.get("request_id") or ""),
            source_turn=context.turn() if callable(context.turn) else None,
            workspace=context.workspace)
        try:
            result = await context.service.consult(request, quiesce=context.quiesce)
        except ConsultRefused as exc:
            return ToolResult(tool_call_id="", is_error=True, content=str(exc))
        return ToolResult(tool_call_id="", is_error=result.outcome != "answered",
                          content=result.tool_text(),
                          metadata={"consult": {"request_id": result.receipt.get("request_id"),
                                                "outcome": result.outcome, "replay": result.replay}})
