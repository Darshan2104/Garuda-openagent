import hashlib
from pathlib import PurePosixPath

from garuda.context.outline import PythonOutlineCache
from garuda.core.artifact_read_model import source_capture
from garuda.tools.protocol import ToolContext, ToolEffect
from garuda.tools.source_version import (
    UNKNOWN_VERSION_NOTE,
    digest_version_token,
    version_token,
    versioned_reads,
)
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
        "Set bounded=true to require bounded backend I/O (unsupported backends refuse). "
        "Set outline=true for host-parsed Python AST metadata with source identity and hidden "
        "body ranges, instead of source text. Read the relevant raw ranges before editing. "
        "Outline reads the whole file and cannot be combined with offset/limit/tail/bounded."
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
            "outline": {
                "type": "boolean",
                "description": "Return Python (.py/.pyi) AST metadata only (default false)",
            },
        },
        "required": ["path"],
    }

    def __init__(self):
        self._outlines = PythonOutlineCache()

    async def execute(
        self,
        arguments: dict,
        env: Environment,
        ctx: ToolContext,
    ) -> ToolResult:
        path = arguments["path"]
        outline = arguments.get("outline", False)
        # An outline parses the whole source, so it never takes the paged path.
        if type(outline) is not bool or (
            outline and {"offset", "limit", "tail", "bounded"} & arguments.keys()
        ):
            return ToolResult(
                tool_call_id="", is_error=True,
                content=("outline must be a boolean and outline=true cannot be combined with "
                         "offset/limit/tail/bounded."),
                metadata={"artifact_source": source_capture(kind="tool_observation", completeness="unknown")},
            )
        offset = arguments.get("offset") or 1
        limit = arguments.get("limit") or DEFAULT_READ_LIMIT
        if offset < 1:
            offset = 1
        if limit < 1:
            limit = 1

        page = content = None
        metadata = {"bounded_read": False}
        tail = arguments.get("tail", False)
        require_bounded = arguments.get("bounded", False) or tail
        # Under the versioned edit contract an ordinary page streams the whole
        # file once more to hash it (bounded memory), so its token names the
        # full content an edit will compare. An explicit tail/bounded read keeps
        # its I/O bound and reports a version only if it covered the whole file.
        want_digest = versioned_reads(ctx) and not require_bounded
        try:
            # Outlines parse full text, so they skip the page reader (tail/bounded
            # were refused above, so require_bounded is false here).
            if not outline and isinstance(env, BoundedTextEnvironment):
                try:
                    page = await env.read_text_page(path, offset=offset,
                                                    limit=min(limit, MAX_PAGE_LINES), tail=tail,
                                                    digest=want_digest)
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

        if outline:
            if PurePosixPath(path).suffix.lower() not in {".py", ".pyi"}:
                digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
                output = (
                    "Python outline unavailable: unsupported language (requires .py or .pyi). "
                    "No outline returned.\n"
                    f"source_sha256: {digest} (UTF-8 of Environment.read_file text)\n"
                    "Use read_file without outline, with offset/limit for raw source."
                )
            else:
                result = self._outlines.render(
                    content, max_lines=DEFAULT_READ_LIMIT, max_line_chars=MAX_LINE_CHARS,
                )
                digest, output = result.sha256, result.content
            return ToolResult(
                tool_call_id="", content=output,
                metadata={"artifact_source": source_capture(
                    kind="tool_observation", completeness="partial", identity=f"sha256:{digest}",
                )},
            )

        if page is None:
            lines = content.splitlines()
            total = len(lines)
            start = offset - 1
            selected = lines[start : start + limit]
        else:
            total = page.total_lines
            selected = page.lines
            if page.offset is None:
                # A tail of a large file: line numbers are unknown, never guessed.
                body = "\n".join(line if len(line) <= MAX_LINE_CHARS
                                 else line[:MAX_LINE_CHARS] + "... (line truncated)"
                                 for line in selected)
                return _versioned(
                    f"{body}\n(last {len(selected)} lines; line numbers and total lines unknown)",
                    metadata, ctx, env, path, page=page)
            offset = page.offset
            start = offset - 1
        if total == 0:
            # An empty file still has a version: the empty text is the full content.
            return _versioned("(empty file)", metadata, ctx, env, path, content=content, page=page)
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
        return _versioned(output, metadata, ctx, env, path,
                          content=content, page=page)


def _versioned(output: str, metadata: dict, ctx: ToolContext, env: Environment, path: str, *,
               content: str | None = None, page=None) -> ToolResult:
    """Attach the source version of the FULL content, never of the lines shown.

    A whole read hashes its text; a bounded page supplies the digest its reader
    took over the whole file. When neither exists the version is unknown (None)
    so a versioned edit is refused instead of silently mismatching.
    """
    if content is not None:
        token = version_token(ctx, env, path, content)
    elif page is not None and page.content_sha256:
        token = digest_version_token(ctx, env, path, "sha256:" + page.content_sha256)
    else:
        token = None
    if versioned_reads(ctx):
        output += f"\n(version: {token})" if token else f"\n{UNKNOWN_VERSION_NOTE}"
    return ToolResult(tool_call_id="", content=output, metadata={**metadata, "source_version": token})


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

        metadata = {}
        message += await post_edit_report(env, path, ctx, metadata=metadata)
        return ToolResult(tool_call_id="", content=message, metadata=metadata)
