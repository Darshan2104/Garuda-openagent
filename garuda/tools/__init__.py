from garuda.tools.bash import BashTool
from garuda.tools.files import ReadFileTool, WriteFileTool
from garuda.tools.image_read import ImageReadTool
from garuda.tools.patch import ApplyPatchTool
from garuda.tools.protocol import Tool
from garuda.tools.task_complete import TaskCompleteTool
from garuda.tools.tmux import TmuxCaptureTool, TmuxExecTool

__all__ = [
    "ApplyPatchTool",
    "BashTool",
    "ImageReadTool",
    "ReadFileTool",
    "TaskCompleteTool",
    "TmuxCaptureTool",
    "TmuxExecTool",
    "WriteFileTool",
    "default_tools",
    "tools_for_names",
]

_TOOL_REGISTRY: dict[str, Tool] = {
    "bash": BashTool(),
    "read_file": ReadFileTool(),
    "write_file": WriteFileTool(),
    "apply_patch": ApplyPatchTool(),
    "task_complete": TaskCompleteTool(),
    "tmux_exec": TmuxExecTool(),
    "tmux_capture": TmuxCaptureTool(),
    "image_read": ImageReadTool(),
}


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
            "task_complete",
        ]
    )


def tools_for_names(names: list[str] | None) -> list[Tool]:
    if names is None:
        return list(_TOOL_REGISTRY.values())
    return [_TOOL_REGISTRY[name] for name in names if name in _TOOL_REGISTRY]
