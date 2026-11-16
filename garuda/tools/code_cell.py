"""Opt-in short Python cells with a runner-owned tool bridge."""

import asyncio
import json
import math
import time

from garuda.tools.protocol import ToolContext, ToolEffect
from garuda.types import ToolResult
from garuda.workspace.cells import CellEnvironment, CellError, CellLimits, exchange_cell, uncharged
from garuda.workspace.health import HealthMonitoredEnvironment
from garuda.workspace.protocol import Environment


def _limit_problem(value, ceiling: float, *, integer=False) -> str | None:
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or not 0 < value <= ceiling
            or (integer and not isinstance(value, int))):
        return f"must be a positive {'integer' if integer else 'number'} <= {ceiling}"
    return None


class CodeCellTool:
    opt_in = True
    name = "code_cell"
    effect = ToolEffect.MUTATING
    description = (
        "Run a short Python cell in a confined scratch process. Opt-in; requires Docker "
        "or Linux/bwrap. inputs is a JSON object; assign result for a JSON result. "
        "tool(name, arguments) returns {content, is_error} through ordinary Garuda screening. "
        "No workspace mounts, network, dependency installation or retained kernel."
    )
    options_schema = {
        "timeout": lambda value: _limit_problem(value, 30.0),
        "output_bytes": lambda value: _limit_problem(value, 65_536, integer=True),
        "nested_calls": lambda value: _limit_problem(value, 32, integer=True),
    }
    parameters = {
        "type": "object",
        "properties": {
            "code": {"type": "string"},
            "inputs": {"type": "object"},
        },
        "required": ["code"],
        "additionalProperties": False,
    }

    async def execute(self, arguments: dict, env: Environment, ctx: ToolContext) -> ToolResult:
        def error(message):
            return ToolResult(tool_call_id="", content=message, is_error=True)

        # The loop wraps every run's environment in its health monitor, whose
        # attribute forwarding a runtime-checkable protocol does not see. The
        # confinement owner is the wrapped backend, as for the debug tools.
        while isinstance(env, HealthMonitoredEnvironment):
            env = env.inner
        if not isinstance(env, CellEnvironment):
            return error("Code cells unsupported: this environment has no proved confinement")
        if ctx.cell_bridge is None:
            return error("Code cells unsupported: ordinary ToolRunner bridge is unavailable")
        code, inputs = arguments.get("code"), arguments.get("inputs", {})
        if not isinstance(code, str) or not isinstance(inputs, dict):
            return error("Cell code must be a string and inputs must be an object")
        try:
            request_bytes = len(json.dumps({"code": code, "inputs": inputs}, allow_nan=False).encode())
        except (TypeError, ValueError):
            return error("Cell inputs must be JSON serializable")
        if request_bytes > 32_768:
            return error("Cell code/input byte limit exceeded (32768 bytes)")
        options = {
            "timeout": 10.0, "output_bytes": 32_768, "nested_calls": 8,
        }
        values = {}
        for name, default in options.items():
            value = ctx.option(self.name, name, default)
            problem = self.options_schema[name](value)
            if problem:
                return error(f"Invalid cell {name}: {problem}")
            values[name] = value
        if ctx.deadline_monotonic is not None:
            values["timeout"] = min(values["timeout"], ctx.deadline_monotonic - time.monotonic())
            if values["timeout"] <= 0:
                return error("Cell wall-time budget exhausted")
        loop = asyncio.get_running_loop()
        resume_by = None
        if ctx.deadline_monotonic is not None:
            resume_by = loop.time() + ctx.deadline_monotonic - time.monotonic()
        try:
            # Admission, launch, the cell's own code and delivery share one budget.
            # Nested calls are excluded: each is governed by its own tool timeout
            # and approval, while the run deadline still caps the remaining budget.
            async with asyncio.timeout(values["timeout"]) as budget:
                async def bridge(name: str, arguments: dict, index: int) -> dict:
                    async with uncharged(budget, resume_by=resume_by):
                        return await ctx.cell_bridge(name, arguments, index)

                async with env.open_code_cell() as process:
                    result = await exchange_cell(
                        process, code, inputs, CellLimits(**values), bridge,
                    )
            content = json.dumps(result, ensure_ascii=False)
            if len(content.encode("utf-8")) > values["output_bytes"]:
                return error("Cell output byte limit exceeded")
            return ToolResult(
                tool_call_id="", content=content,
                is_error="error" in result,
            )
        except (CellError, TimeoutError, OSError) as exc:
            if asyncio.current_task().cancelling():
                raise asyncio.CancelledError from exc
            return error(str(exc) or "Cell wall-time limit exceeded; no replay")
