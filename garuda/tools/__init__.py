from __future__ import annotations

# NB: `garuda.mcp.client` is imported lazily inside build_toolkit / __getattr__.
# Importing it here would create a cycle: mcp.client -> tools.protocol -> tools
# (package __init__) -> mcp.client (partially initialized).
import logging
from typing import TYPE_CHECKING

from garuda.tools.background import BashBackgroundTool, KillTaskTool, TaskOutputTool
from garuda.tools.bash import BashTool
from garuda.tools.buffer_tools import (
    BufferGrepTool,
    BufferListTool,
    BufferQueryTool,
    BufferSliceTool,
)
from garuda.tools.documents import ReadPdfTool, ReadSpreadsheetTool
from garuda.tools.edit import EditTool
from garuda.tools.files import ReadFileTool, WriteFileTool
from garuda.tools.image_read import ImageReadTool
from garuda.tools.protocol import Tool
from garuda.tools.registry import (
    ToolRegistry,
    builtin_registry,
    list_tool_names,
    register_tool,
    tools_for_names,
)
from garuda.tools.search import GlobTool, GrepTool, LsTool
from garuda.tools.subagent import InvokeSubagentTool
from garuda.tools.task_complete import TaskCompleteTool
from garuda.tools.tmux import TmuxCaptureTool, TmuxExecTool
from garuda.tools.todo import TodoTool
from garuda.tools.web import WebFetchTool, WebSearchTool

if TYPE_CHECKING:
    from garuda.mcp.client import McpClientManager

logger = logging.getLogger(__name__)

__all__ = [
    "BashBackgroundTool",
    "BashTool",
    "BufferGrepTool",
    "BufferListTool",
    "BufferQueryTool",
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
    "ToolRegistry",
    "WebFetchTool",
    "WebSearchTool",
    "WriteFileTool",
    "build_toolkit",
    "builtin_registry",
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
        BufferQueryTool(),
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
            "buffer_query",
            "tmux_exec",
            "tmux_capture",
            "image_read",
            "read_pdf",
            "read_spreadsheet",
            "invoke_subagent",
            "task_complete",
        ]
    )


def _resolve_project_tools(workspace, override: bool | None) -> list[Tool]:
    """Load `.agent/tools/*.py` custom tools when opt-in is enabled.

    ``override`` (a CLI/SDK flag) wins over the ``settings.yaml`` value when set;
    ``None`` defers to the project's ``load_project_tools`` setting. Importing the
    modules executes repo code, hence the opt-in gate.
    """
    from garuda.config.agent_home import resolve_agent_home

    home = resolve_agent_home(workspace)
    enabled = home.load_project_tools if override is None else override
    if not enabled:
        return []
    from garuda.tools.project_loader import load_project_tools

    tools = load_project_tools(home.tools_dirs)
    if tools:
        logger.warning(
            "Loaded %d project tool(s) from .agent/tools (load_project_tools enabled): %s",
            len(tools),
            ", ".join(t.name for t in tools),
        )
    return tools


async def build_toolkit(
    names: list[str] | None,
    mcp_config_path: str | list[str] | None = None,
    *,
    extra_tools: list[Tool] | None = None,
    registry: "ToolRegistry | None" = None,
    workspace: str | None = None,
    load_project_tools: bool | None = None,
) -> tuple[list[Tool], "McpClientManager | None"]:
    """Resolve a run's tool list from names, custom tools, and MCP servers.

    Built-ins resolve from the shared base registry. When ``extra_tools`` (custom
    tools) are given, ``workspace`` yields opt-in ``.agent/tools`` modules, or an
    explicit ``registry`` is passed — selection happens against a per-run *layer*
    so these additions never mutate the shared base or leak into other runs.
    """
    from garuda.mcp.client import McpClientManager

    if registry is None:
        combined = list(extra_tools or [])
        if workspace is not None:
            combined += _resolve_project_tools(workspace, load_project_tools)
        if combined:
            registry = builtin_registry().layer()
            for tool in combined:
                registry.register(tool, replace=True)
        else:
            registry = builtin_registry()
    tools = registry.select(names)
    manager: McpClientManager | None = None
    # Accept either a single path (back-compat) or an ordered list of paths to
    # merge (project + global). Empty/None means MCP stays disabled.
    if isinstance(mcp_config_path, str):
        paths = [mcp_config_path]
    else:
        paths = [p for p in (mcp_config_path or []) if p]
    if paths:
        manager = await McpClientManager.from_paths(paths)
        tools = tools + manager.get_tools()
    return tools, manager


def __getattr__(name: str):
    # Lazy re-export so `from garuda.tools import McpClientManager` still works
    # without eagerly importing mcp.client at package load (avoids the cycle).
    if name == "McpClientManager":
        from garuda.mcp.client import McpClientManager

        return McpClientManager
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
