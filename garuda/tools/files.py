from garuda.tools.protocol import ToolContext
from garuda.types import ToolResult
from garuda.workspace.protocol import Environment

DEFAULT_READ_LIMIT = 2000
MAX_LINE_CHARS = 2000


class ReadFileTool:
    name = "read_file"
    description = (
        "Read a text file from the workspace. Output is numbered like `cat -n`. "
        "Use offset (1-based start line) and limit (max lines) to page through large files."
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "File path relative to workspace or absolute",
            },
            "offset": {
                "type": "integer",
                "description": "1-based line number to start reading from (default 1)",
            },
            "limit": {
                "type": "integer",
                "description": f"Maximum number of lines to read (default {DEFAULT_READ_LIMIT})",
            },
        },
        "required": ["path"],
    }

    async def execute(
        self,
        arguments: dict,
        env: Environment,
        ctx: ToolContext,
    ) -> ToolResult:
        path = arguments["path"]
        offset = arguments.get("offset") or 1
        limit = arguments.get("limit") or DEFAULT_READ_LIMIT
        if offset < 1:
            offset = 1
        if limit < 1:
            limit = 1

        try:
            content = await env.read_file(path)
        except (FileNotFoundError, IsADirectoryError, OSError) as exc:
            return ToolResult(
                tool_call_id="",
                content=f"Cannot read {path}: {exc}",
                is_error=True,
            )

        lines = content.splitlines()
        total = len(lines)
        if total == 0:
            return ToolResult(tool_call_id="", content="(empty file)")
        if offset > total:
            return ToolResult(
                tool_call_id="",
                content=f"offset {offset} is beyond end of file ({total} lines total)",
                is_error=True,
            )

        start = offset - 1
        selected = lines[start : start + limit]
        width = len(str(start + len(selected)))
        rendered: list[str] = []
        for i, line in enumerate(selected, start=offset):
            if len(line) > MAX_LINE_CHARS:
                line = line[:MAX_LINE_CHARS] + "... (line truncated)"
            rendered.append(f"{i:>{width}}\t{line}")
        output = "\n".join(rendered)

        shown_start = offset
        shown_end = offset + len(selected) - 1
        if total > len(selected):
            output += (
                f"\n(file has {total} lines total; showing {shown_start}-{shown_end}"
                " — use offset/limit to read more)"
            )
        return ToolResult(tool_call_id="", content=output)


class WriteFileTool:
    name = "write_file"
    description = "Write content to a file in the workspace."
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "File path relative to workspace or absolute"},
            "content": {"type": "string", "description": "Full file content to write"},
        },
        "required": ["path", "content"],
    }

    async def execute(
        self,
        arguments: dict,
        env: Environment,
        ctx: ToolContext,
    ) -> ToolResult:
        await env.write_file(arguments["path"], arguments["content"])
        size = len(arguments["content"].encode("utf-8"))
        return ToolResult(tool_call_id="", content=f"Wrote {arguments['path']} ({size} bytes)")
