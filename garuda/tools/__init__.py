from garuda.mcp.client import McpClientManager
from garuda.tools.bash import BashTool
from garuda.tools.documents import ReadPdfTool, ReadSpreadsheetTool
from garuda.tools.files import ReadFileTool, WriteFileTool
from garuda.tools.image_read import ImageReadTool
from garuda.tools.patch import ApplyPatchTool
from garuda.tools.protocol import Tool
from garuda.tools.registry import list_tool_names, register_tool, tools_for_names
from garuda.tools.subagent import InvokeSubagentTool
from garuda.tools.task_complete import TaskCompleteTool
from garuda.tools.tmux import TmuxCaptureTool, TmuxExecTool

__all__ = [
    "ApplyPatchTool",
    "BashTool",
    "ImageReadTool",
    "InvokeSubagentTool",
    "McpClientManager",
    "ReadFileTool",
    "ReadPdfTool",
    "ReadSpreadsheetTool",
    "TaskCompleteTool",
    "TmuxCaptureTool",
    "TmuxExecTool",
    "WriteFileTool",
    "build_toolkit",
    "default_tools",
    "list_tool_names",
    "register_tool",
    "tools_for_names",
]


def _bootstrap_registry() -> None:
    for tool in [
        BashTool(),
        ReadFileTool(),
        WriteFileTool(),
        ApplyPatchTool(),
        TaskCompleteTool(),
        TmuxExecTool(),
        TmuxCaptureTool(),
        ImageReadTool(),
        ReadPdfTool(),
        ReadSpreadsheetTool(),
        InvokeSubagentTool(),
    ]:
        register_tool(tool, replace=True)


_bootstrap_registry()


def default_tools() -> list[Tool]:
    return tools_for_names(
        [
            "bash",
            "read_file",
            "write_file",
            "apply_patch",
            "tmux_exec",
            "tmux_capture",
            "image_read",
            "read_pdf",
            "read_spreadsheet",
            "invoke_subagent",
            "task_complete",
        ]
    )


async def build_toolkit(
    names: list[str] | None,
    mcp_config_path: str | None = None,
) -> tuple[list[Tool], McpClientManager | None]:
    tools = tools_for_names(names)
    manager: McpClientManager | None = None
    if mcp_config_path:
        manager = await McpClientManager.from_config(mcp_config_path)
        tools = tools + manager.get_tools()
    return tools, manager
