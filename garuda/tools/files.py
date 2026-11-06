from garuda.tools.protocol import ToolContext, ToolEffect
from garuda.types import ToolResult
from garuda.workspace.protocol import Environment, WorkspaceAccessDenied
from garuda.workspace.text_reads import MAX_LINE_CHARS, MAX_PAGE_LINES, BoundedTextEnvironment

DEFAULT_READ_LIMIT = 2000


class ReadFileTool:
    effect = ToolEffect.READ_ONLY
    name = "read_file"
    description = (
        "Read a text file from the workspace. Output is numbered like `cat -n`. "
        "Use offset (1-based start line) and limit (max 2000 lines), or tail=true. "
        "Set bounded=true to require bounded backend I/O (unsupported backends refuse)."
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "File path relative to workspace or absolute",
            },
            "bounded": {"type": "boolean", "description": "Require bounded I/O; refuse unsupported backends"},
            "tail": {"type": "boolean", "description": "Return the last limit lines; cannot combine with offset"},
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

        page = None
        metadata = {"bounded_read": False}
        tail = arguments.get("tail", False)
        require_bounded = arguments.get("bounded", False) or tail
        try:
            if isinstance(env, BoundedTextEnvironment):
                try:
                    page = await env.read_text_page(path, offset=offset,
                                                    limit=min(limit, MAX_PAGE_LINES), tail=tail)
                except NotImplementedError:
                    if require_bounded:
                        raise
            elif require_bounded:
                raise NotImplementedError("Bounded text reads are not supported by this backend")
            if page is None:
                content = await env.read_file(path)
            else:
                metadata = {"bounded_read": True, "bytes_read": page.bytes_read,
                            "total_lines": page.total_lines, "has_more": page.has_more}
        except (NotImplementedError, ValueError) as exc:
            return ToolResult(tool_call_id="", content=f"Cannot read {path}: {exc}", is_error=True)
        except (FileNotFoundError, IsADirectoryError, OSError) as exc:
            return ToolResult(
                tool_call_id="",
                content=f"Cannot read {path}: {exc}",
                is_error=True,
                metadata={"permission_denied": True} if isinstance(exc, WorkspaceAccessDenied) else {},
            )

        if page is None:
            lines = content.splitlines()
            total = len(lines)
            start = offset - 1
            selected = lines[start : start + limit]
        else:
            total = page.total_lines
            offset = page.offset
            start = offset - 1
            selected = page.lines
        if total == 0:
            return ToolResult(tool_call_id="", content="(empty file)", metadata=metadata)
        if not selected:
            return ToolResult(
                tool_call_id="",
                content=f"offset {offset} is beyond end of file ({total} lines total)",
                is_error=True, metadata=metadata,
            )

        width = len(str(start + len(selected)))
        rendered: list[str] = []
        for i, line in enumerate(selected, start=offset):
            if len(line) > MAX_LINE_CHARS:
                line = line[:MAX_LINE_CHARS] + "... (line truncated)"
            rendered.append(f"{i:>{width}}\t{line}")
        output = "\n".join(rendered)

        shown_start = offset
        shown_end = offset + len(selected) - 1
        if total is None:
            output += f"\n(total lines unknown; showing {shown_start}-{shown_end} — use offset/limit to read more)"
        elif total > len(selected):
            output += (
                f"\n(file has {total} lines total; showing {shown_start}-{shown_end}"
                " — use offset/limit to read more)"
            )
        return ToolResult(tool_call_id="", content=output, metadata=metadata)


class WriteFileTool:
    effect = ToolEffect.MUTATING
    name = "write_file"
    description = (
        "Write content to a file in the workspace, replacing it entirely. "
        "Use this for new files, or when replacing a whole small file. "
        "To change part of an existing file use edit (or multi_edit for several "
        "changes at once): a full rewrite costs tokens in proportion to the file's "
        "size and silently drops anything you omit."
    )
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
        content = arguments["content"]
        path = arguments["path"]
        await env.write_file(path, content)
        size = len(content.encode("utf-8"))
        line_count = content.count("\n") + (1 if content and not content.endswith("\n") else 0)
        message = f"Wrote {path} ({size} bytes, {line_count} lines)"
        from garuda.tools.diagnostics import post_edit_report

        message += await post_edit_report(env, path, ctx)
        return ToolResult(tool_call_id="", content=message)
