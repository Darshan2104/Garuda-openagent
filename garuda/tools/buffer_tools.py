"""Retrieval tools over the tool-output buffer (RLM-style).

When a tool's output is large it is stored in a `ToolOutputBuffer` and only a stub
enters the context. These tools let the model pull back exactly the lines it needs.
"""

from garuda.tools.protocol import ToolContext
from garuda.types import ToolResult
from garuda.workspace.protocol import Environment

DEFAULT_BUFFER_GREP_MAX = 100


def _no_buffer() -> ToolResult:
    return ToolResult(
        tool_call_id="",
        content="Output buffering is not enabled for this session.",
        is_error=True,
    )


class BufferGrepTool:
    name = "buffer_grep"
    description = (
        "Search a stored tool-output buffer for a regular expression. Returns matching "
        "lines as line:content. Use the buffer_id from a [buffer:...] stub in the conversation."
    )
    parameters = {
        "type": "object",
        "properties": {
            "buffer_id": {"type": "string", "description": "Buffer id from a [buffer:...] stub"},
            "pattern": {"type": "string", "description": "Regular expression to search for"},
            "max_results": {
                "type": "integer",
                "description": f"Maximum matching lines (default {DEFAULT_BUFFER_GREP_MAX})",
            },
        },
        "required": ["buffer_id", "pattern"],
    }

    async def execute(self, arguments: dict, env: Environment, ctx: ToolContext) -> ToolResult:
        buffer = getattr(ctx, "buffer", None)
        if buffer is None:
            return _no_buffer()
        buffer_id = arguments["buffer_id"]
        pattern = arguments["pattern"]
        max_results = arguments.get("max_results") or DEFAULT_BUFFER_GREP_MAX
        try:
            matches = buffer.grep(buffer_id, pattern, max_results=max_results)
        except KeyError as exc:
            return ToolResult(tool_call_id="", content=str(exc), is_error=True)
        except Exception as exc:  # noqa: BLE001 - bad regex, etc.
            return ToolResult(
                tool_call_id="", content=f"buffer_grep failed: {type(exc).__name__}: {exc}", is_error=True
            )
        if not matches:
            return ToolResult(tool_call_id="", content=f"No matches for {pattern} in buffer {buffer_id}")
        capped = len(matches) >= max_results
        out = "\n".join(matches)
        if capped:
            out += f"\n(results capped at {max_results})"
        return ToolResult(tool_call_id="", content=out)


class BufferSliceTool:
    name = "buffer_slice"
    description = (
        "Read a line range from a stored tool-output buffer. Returns lines as line:content. "
        "Use the buffer_id from a [buffer:...] stub in the conversation."
    )
    parameters = {
        "type": "object",
        "properties": {
            "buffer_id": {"type": "string", "description": "Buffer id from a [buffer:...] stub"},
            "start_line": {"type": "integer", "description": "First line (1-based, inclusive)"},
            "end_line": {"type": "integer", "description": "Last line (1-based, inclusive)"},
        },
        "required": ["buffer_id", "start_line", "end_line"],
    }

    async def execute(self, arguments: dict, env: Environment, ctx: ToolContext) -> ToolResult:
        buffer = getattr(ctx, "buffer", None)
        if buffer is None:
            return _no_buffer()
        try:
            content = buffer.slice(
                arguments["buffer_id"], int(arguments["start_line"]), int(arguments["end_line"])
            )
        except KeyError as exc:
            return ToolResult(tool_call_id="", content=str(exc), is_error=True)
        return ToolResult(tool_call_id="", content=content or "(no lines in range)")


class BufferListTool:
    name = "buffer_list"
    description = "List the stored tool-output buffers for this session (id, size, lines, source tool)."
    parameters = {"type": "object", "properties": {}, "required": []}

    async def execute(self, arguments: dict, env: Environment, ctx: ToolContext) -> ToolResult:
        buffer = getattr(ctx, "buffer", None)
        if buffer is None:
            return _no_buffer()
        refs = buffer.list_buffers()
        if not refs:
            return ToolResult(tool_call_id="", content="No buffers stored for this session.")
        lines = [
            f"{r.buffer_id} | {r.size_bytes} bytes | {r.line_count} lines"
            + (f" | tool={r.tool_name}" if r.tool_name else "")
            for r in refs
        ]
        return ToolResult(tool_call_id="", content="\n".join(lines))
