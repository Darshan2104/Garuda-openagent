from garuda.tools.protocol import ToolContext
from garuda.types import ToolResult
from garuda.workspace.protocol import Environment


class ReadFileTool:
    name = "read_file"
    description = "Read a text file from the workspace."
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "File path relative to workspace or absolute"},
        },
        "required": ["path"],
    }

    async def execute(
        self,
        arguments: dict,
        env: Environment,
        ctx: ToolContext,
    ) -> ToolResult:
        content = await env.read_file(arguments["path"])
        return ToolResult(tool_call_id="", content=content)


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
        return ToolResult(tool_call_id="", content=f"Wrote {arguments['path']}")
