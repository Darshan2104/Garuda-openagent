from garuda.tools.bash import BashTool
from garuda.tools.files import ReadFileTool, WriteFileTool
from garuda.tools.patch import ApplyPatchTool
from garuda.tools.protocol import Tool
from garuda.tools.task_complete import TaskCompleteTool

__all__ = [
    "ApplyPatchTool",
    "BashTool",
    "ReadFileTool",
    "TaskCompleteTool",
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
}


def default_tools() -> list[Tool]:
    return tools_for_names(["bash", "read_file", "write_file", "apply_patch", "task_complete"])


def tools_for_names(names: list[str] | None) -> list[Tool]:
    if names is None:
        return list(_TOOL_REGISTRY.values())
    return [_TOOL_REGISTRY[name] for name in names if name in _TOOL_REGISTRY]
