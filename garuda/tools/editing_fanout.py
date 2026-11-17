"""Run-scoped tool adapter; trusted assembly supplies its service bounds."""

from copy import deepcopy

from garuda.core.child_result import canonical_json
from garuda.core.editing_fanout import MAX_TASK_CHARS, TOOL_NAME, run_fanout
from garuda.tools.protocol import ToolEffect
from garuda.types import ToolResult


class FanOutEditingWorkersTool:
    name = TOOL_NAME
    effect = ToolEffect.MUTATING
    description = (
        "Run a bounded batch of editing workers concurrently in isolated local repositories. "
        "Each task needs a profile with an output schema. Returns typed receipts in request "
        "order. With integrate='in_order', ready candidates are checked and applied one at "
        "a time; later siblings may be stale_base."
    )
    parameters = {
        "type": "object",
        "properties": {
            "tasks": {
                "type": "array", "minItems": 1,
                "items": {
                    "type": "object",
                    "properties": {
                        "profile": {"type": "string", "minLength": 1},
                        "task": {"type": "string", "minLength": 1, "maxLength": MAX_TASK_CHARS},
                    },
                    "required": ["profile", "task"], "additionalProperties": False,
                },
            },
            "integrate": {"type": "string", "enum": ["none", "in_order"], "default": "none"},
        },
        "required": ["tasks"], "additionalProperties": False,
    }

    @classmethod
    def for_service(cls, max_concurrent: int, profiles: list[str] | None):
        tool = cls()
        tool.parameters = deepcopy(cls.parameters)
        tasks = tool.parameters["properties"]["tasks"]
        tasks["maxItems"] = max_concurrent
        if profiles is not None:
            tasks["items"]["properties"]["profile"] = {"type": "string", "enum": list(profiles)}
        return tool

    async def execute(self, arguments, env, ctx):
        budget = ctx.output_budget(False) if ctx.output_budget else 16_000
        if ctx.buffer is not None:
            budget = min(budget, ctx.buffer.threshold_bytes)
        if budget < 2:
            # There is no valid JSON object in a zero/one-byte budget. Refuse
            # before orchestration rather than clip a receipt after execution.
            return ToolResult("", "", is_error=True)
        payload, error = await run_fanout(
            ctx.subagent_runner, arguments, env=env, session_id=ctx.session_id,
            permissions=ctx.permissions, output_budget=budget,
        )
        content = canonical_json(payload)
        return ToolResult("", content, is_error=error)
