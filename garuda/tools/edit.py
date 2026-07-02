from garuda.tools.protocol import ToolContext
from garuda.types import ToolResult
from garuda.workspace.protocol import Environment

_SNIPPET_CONTEXT_LINES = 2


def _snippet_around(content: str, position: int) -> str:
    """Return a few lines of `content` surrounding character offset `position`."""
    lines = content.splitlines()
    if not lines:
        return ""
    # Find the line index containing `position`.
    offset = 0
    line_index = 0
    for i, line in enumerate(lines):
        end = offset + len(line) + 1  # account for the newline
        if position < end:
            line_index = i
            break
        offset = end
    else:
        line_index = len(lines) - 1
    start = max(0, line_index - _SNIPPET_CONTEXT_LINES)
    stop = min(len(lines), line_index + _SNIPPET_CONTEXT_LINES + 1)
    return "\n".join(lines[start:stop])


class EditTool:
    name = "edit"
    description = (
        "Perform an exact string replacement in an existing file. "
        "old_string must match the file contents exactly (including whitespace) and, "
        "unless replace_all is true, must be unique in the file — include enough "
        "surrounding context to disambiguate. To create a new file, use write_file instead."
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "File path relative to workspace or absolute",
            },
            "old_string": {
                "type": "string",
                "description": "Exact text to replace (must exist in the file)",
            },
            "new_string": {
                "type": "string",
                "description": "Text to replace old_string with",
            },
            "replace_all": {
                "type": "boolean",
                "description": "Replace every occurrence of old_string (default false)",
                "default": False,
            },
        },
        "required": ["path", "old_string", "new_string"],
    }

    async def execute(
        self,
        arguments: dict,
        env: Environment,
        ctx: ToolContext,
    ) -> ToolResult:
        path = arguments["path"]
        old_string = arguments["old_string"]
        new_string = arguments["new_string"]
        replace_all = bool(arguments.get("replace_all", False))

        if old_string == new_string:
            return ToolResult(
                tool_call_id="",
                content="old_string and new_string are identical — nothing to change.",
                is_error=True,
            )

        try:
            content = await env.read_file(path)
        except (FileNotFoundError, IsADirectoryError, OSError) as exc:
            return ToolResult(
                tool_call_id="",
                content=(
                    f"Cannot edit {path}: file could not be read ({exc}). "
                    "If you want to create a new file, use the write_file tool instead."
                ),
                is_error=True,
            )

        count = content.count(old_string)
        if count == 0:
            return ToolResult(
                tool_call_id="",
                content=f"old_string not found in {path}",
                is_error=True,
            )
        if count > 1 and not replace_all:
            return ToolResult(
                tool_call_id="",
                content=(
                    f"old_string occurs {count} times in {path}. "
                    "Add more surrounding context to make it unique, "
                    "or set replace_all to true to replace every occurrence."
                ),
                is_error=True,
            )

        if replace_all:
            updated = content.replace(old_string, new_string)
            replacements = count
        else:
            updated = content.replace(old_string, new_string, 1)
            replacements = 1

        await env.write_file(path, updated)

        first_site = updated.find(new_string) if new_string else content.find(old_string)
        snippet = _snippet_around(updated, max(first_site, 0))
        plural = "s" if replacements != 1 else ""
        message = f"Edited {path} ({replacements} replacement{plural})"
        if snippet:
            message += f"\n\nSnippet of new content:\n{snippet}"
        return ToolResult(tool_call_id="", content=message)
