from __future__ import annotations

# NB: `garuda.mcp.client` is imported lazily inside build_toolkit / __getattr__.
# Importing it here would create a cycle: mcp.client -> tools.protocol -> tools
# (package __init__) -> mcp.client (partially initialized).
from typing import TYPE_CHECKING

from garuda.tools.background import BashBackgroundTool, KillTaskTool, TaskOutputTool
from garuda.tools.bash import BashTool
from garuda.tools.buffer_tools import BufferGrepTool, BufferListTool, BufferSliceTool
from garuda.tools.documents import ReadPdfTool, ReadSpreadsheetTool
from garuda.tools.edit import EditTool
from garuda.tools.files import ReadFileTool, WriteFileTool
from garuda.tools.image_read import ImageReadTool
from garuda.tools.protocol import Tool
from garuda.tools.registry import list_tool_names, register_tool, tools_for_names
from garuda.tools.search import GlobTool, GrepTool, LsTool
from garuda.tools.subagent import InvokeSubagentTool
from garuda.tools.task_complete import TaskCompleteTool
from garuda.tools.tmux import TmuxCaptureTool, TmuxExecTool
from garuda.tools.todo import TodoTool
from garuda.tools.web import WebFetchTool, WebSearchTool

if TYPE_CHECKING:
    from garuda.mcp.client import McpClientManager

__all__ = [
    "BashBackgroundTool",
    "BashTool",
    "BufferGrepTool",
    "BufferListTool",
    "BufferSliceTool",
    "EditTool",
    "KillTaskTool",
    "TaskOutputTool",
    "GlobTool",
    "GrepTool",
    "ImageReadTool",
    "InvokeSubagentTool",
    "LsTool",
    "McpClientManager",
    "ReadFileTool",
    "ReadPdfTool",
    "ReadSpreadsheetTool",
    "TaskCompleteTool",
    "TmuxCaptureTool",
    "TmuxExecTool",
    "TodoTool",
    "WebFetchTool",
    "WebSearchTool",
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
        BashBackgroundTool(),
        TaskOutputTool(),
        KillTaskTool(),
        ReadFileTool(),
        WriteFileTool(),
        EditTool(),
        GrepTool(),
        GlobTool(),
        LsTool(),
        TodoTool(),
        WebFetchTool(),
        WebSearchTool(),
        TaskCompleteTool(),
        TmuxExecTool(),
        TmuxCaptureTool(),
        ImageReadTool(),
        ReadPdfTool(),
        ReadSpreadsheetTool(),
        InvokeSubagentTool(),
        BufferGrepTool(),
        BufferSliceTool(),
        BufferListTool(),
    ]:
        register_tool(tool, replace=True)


_bootstrap_registry()


def default_tools() -> list[Tool]:
    return tools_for_names(
        [
            "bash",
            "bash_background",
            "task_output",
            "kill_task",
            "read_file",
            "write_file",
            "edit",
            "grep",
            "glob",
            "ls",
            "todo",
            "web_fetch",
            "web_search",
            "buffer_grep",
            "buffer_slice",
            "buffer_list",
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
) -> tuple[list[Tool], "McpClientManager | None"]:
    from garuda.mcp.client import McpClientManager

    tools = tools_for_names(names)
    manager: McpClientManager | None = None
    if mcp_config_path:
        manager = await McpClientManager.from_config(mcp_config_path)
        tools = tools + manager.get_tools()
    return tools, manager


def __getattr__(name: str):
    # Lazy re-export so `from garuda.tools import McpClientManager` still works
    # without eagerly importing mcp.client at package load (avoids the cycle).
    if name == "McpClientManager":
        from garuda.mcp.client import McpClientManager

        return McpClientManager
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
