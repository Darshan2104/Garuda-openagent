import re

from garuda.tools.protocol import ToolContext
from garuda.types import ToolResult
from garuda.workspace.protocol import Environment


def apply_unified_patch(original: str, patch_text: str) -> str:
    lines = original.splitlines(keepends=True)
    if not lines and original:
        lines = [original]
    if not lines:
        lines = [""]

    output: list[str] = []
    index = 0
    hunk_header = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")

    for raw_line in patch_text.splitlines():
        if raw_line.startswith("@@"):
            match = hunk_header.match(raw_line)
            if not match:
                continue
            start = int(match.group(1)) - 1
            output.extend(lines[index:start])
            index = start
            continue
        if raw_line.startswith(("---", "+++")):
            continue
        if raw_line.startswith(" "):
            if index < len(lines):
                output.append(lines[index])
            index += 1
        elif raw_line.startswith("-"):
            index += 1
        elif raw_line.startswith("+"):
            addition = raw_line[1:]
            output.append(addition if addition.endswith("\n") else addition + "\n")

    output.extend(lines[index:])
    return "".join(output)


class ApplyPatchTool:
    name = "apply_patch"
    description = "Apply a unified diff patch to a file in the workspace."
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "File path to patch"},
            "patch": {"type": "string", "description": "Unified diff patch content"},
        },
        "required": ["path", "patch"],
    }

    async def execute(
        self,
        arguments: dict,
        env: Environment,
        ctx: ToolContext,
    ) -> ToolResult:
        path = arguments["path"]
        patch_text = arguments["patch"]
        original = await env.read_file(path)
        updated = apply_unified_patch(original, patch_text)
        await env.write_file(path, updated)
        return ToolResult(tool_call_id="", content=f"Patched {path}")
