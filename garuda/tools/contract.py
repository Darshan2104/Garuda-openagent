"""The `contract` tool — resolving the task's acceptance criteria.

The criteria are derived from the task statement at the start of the run and
pinned in context. This tool is how the agent works through them: marking one
verified requires saying what was run and what it showed, which forces the claim
to be attached to evidence at the moment it is made rather than reconstructed in
a summary afterwards.
"""

from garuda.core.contract import (
    ASSUMED,
    UNVERIFIABLE,
    UNVERIFIED,
    VERIFIED,
    AcceptanceContract,
)
from garuda.tools.protocol import ToolContext
from garuda.types import ToolResult
from garuda.workspace.protocol import Environment


class ContractTool:
    name = "contract"
    description = (
        "View and resolve the acceptance criteria derived from the task statement. "
        "Mark a criterion 'verified' once you have run something that would have failed "
        "if it were wrong (say what you ran in the note), 'assumed' when the task left a "
        "value open and you chose one (state the value and why it is reasonable), or "
        "'unverifiable' when nothing in this environment could check it (say why). "
        "Every criterion must be resolved before task_complete will be accepted.\n"
        "RESOLVE THEM IN BATCHES: pass `marks` with every criterion you can settle from "
        "the evidence you already have — one call can resolve all of them. Marking them "
        "one per call wastes a whole turn each, and a task typically has 10-20."
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["view", "mark", "add"],
                "description": "'view' lists criteria, 'mark' sets a status, 'add' records a requirement the extraction missed",
            },
            "marks": {
                "type": "array",
                "description": (
                    "PREFERRED for action='mark': resolve several criteria in one call. "
                    "Each entry is {id, status, note}. Use this instead of one call per "
                    "criterion."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string", "description": "Criterion id, e.g. 'c3'"},
                        "status": {
                            "type": "string",
                            "enum": [VERIFIED, ASSUMED, UNVERIFIABLE, UNVERIFIED],
                        },
                        "note": {
                            "type": "string",
                            "description": "How you established it: what you ran and what it showed.",
                        },
                    },
                    "required": ["id", "status"],
                },
            },
            "id": {
                "type": "string",
                "description": "Single criterion id to mark, e.g. 'c3'. Prefer `marks` for more than one.",
            },
            "status": {
                "type": "string",
                "enum": [VERIFIED, ASSUMED, UNVERIFIABLE, UNVERIFIED],
                "description": "New status for the criterion",
            },
            "note": {
                "type": "string",
                "description": "How you established it: the command you ran and what it showed. Required for verified/assumed/unverifiable.",
            },
            "text": {
                "type": "string",
                "description": "Requirement text (required for action='add')",
            },
        },
        "required": ["action"],
    }

    def __init__(self) -> None:
        # Keyed by session_id: registry tool instances are shared across sessions.
        self._sessions: dict[str, AcceptanceContract] = {}

    def bind(self, session_id: str, contract: AcceptanceContract) -> None:
        self._sessions[session_id] = contract

    def get(self, session_id: str) -> AcceptanceContract | None:
        return self._sessions.get(session_id)

    async def execute(
        self,
        arguments: dict,
        env: Environment,
        ctx: ToolContext,
    ) -> ToolResult:
        contract = self._sessions.get(ctx.session_id) or getattr(ctx, "contract", None)
        if contract is None:
            return ToolResult(
                tool_call_id="",
                content="No acceptance criteria were derived for this task.",
                is_error=True,
            )

        action = (arguments.get("action") or "view").strip().lower()

        if action == "view":
            return ToolResult(tool_call_id="", content=contract.render())

        if action == "add":
            text = (arguments.get("text") or "").strip()
            if not text:
                return ToolResult(
                    tool_call_id="",
                    content="action='add' needs 'text' describing the requirement.",
                    is_error=True,
                )
            criterion = contract.add(text)
            return ToolResult(
                tool_call_id="",
                content=f"Added [{criterion.id}] {criterion.text}\n\n{contract.render()}",
            )

        if action == "mark":
            marks = arguments.get("marks")
            # A single {id,status,note} is folded into the batch path so there is one
            # code path to reason about, and so the contract is rendered once per call
            # rather than once per criterion.
            if not isinstance(marks, list) or not marks:
                criterion_id = (arguments.get("id") or "").strip()
                if not criterion_id:
                    return ToolResult(
                        tool_call_id="",
                        content=(
                            "action='mark' needs either 'marks' (preferred: a list of "
                            "{id, status, note} resolving several criteria at once) or a "
                            "single 'id' (e.g. 'c2')."
                        ),
                        is_error=True,
                    )
                marks = [
                    {
                        "id": criterion_id,
                        "status": (arguments.get("status") or "").strip().lower(),
                        "note": arguments.get("note") or "",
                    }
                ]
            applied, messages = contract.mark_many(marks)
            body = "\n".join(messages)
            # Errors only fail the call when nothing landed; a partially applied batch
            # is progress, and flagging it as an error would invite a full resend.
            return ToolResult(
                tool_call_id="",
                content=f"{body}\n\n{contract.render()}",
                is_error=applied == 0,
            )

        return ToolResult(
            tool_call_id="",
            content=f"Unknown action '{action}'. Use view, mark, or add.",
            is_error=True,
        )
