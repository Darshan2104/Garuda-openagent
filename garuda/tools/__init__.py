from garuda.tools.bash import BashTool
from garuda.tools.files import ReadFileTool, WriteFileTool
from garuda.tools.protocol import Tool

__all__ = ["BashTool", "ReadFileTool", "WriteFileTool", "default_tools"]


def default_tools() -> list[Tool]:
    return [BashTool(), ReadFileTool(), WriteFileTool()]
