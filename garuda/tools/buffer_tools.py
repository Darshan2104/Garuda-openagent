"""Retrieval tools over the tool-output buffer (RLM-style).

When a tool's output is large it is stored in a `ToolOutputBuffer` and only a stub
enters the context. These tools let the model pull back exactly the lines it needs.
"""

from garuda.tools.protocol import ToolContext
from garuda.types import Message, Role, ToolResult
from garuda.workspace.protocol import Environment

DEFAULT_BUFFER_GREP_MAX = 100

# buffer_query (G2) — semantic map-reduce retrieval over a buffer.
DEFAULT_QUERY_MAX_CHUNKS = 8
QUERY_CHUNK_CHARS = 3000
QUERY_MAX_EXCERPT_CHARS = 6000

_MAP_SYSTEM = (
    "You extract the lines from a tool-output excerpt that are relevant to a question. "
    "Return only those lines verbatim, keeping their leading 'lineno:' prefixes. "
    "If nothing in the excerpt is relevant, reply with exactly NONE."
)
_REDUCE_SYSTEM = (
    "You answer a question using only the provided excerpt lines. Be concise and cite the "
    "relevant line numbers. If the excerpts do not answer it, say so."
)


def _chunk_lines(text: str, char_budget: int) -> list[tuple[int, int, str]]:
    """Split text into line-numbered chunks under ``char_budget`` chars each.

    Returns ``(start_line, end_line, body)`` tuples where body is ``lineno:content``
    per line, so retrieved excerpts keep stable line references into the buffer.
    """
    lines = text.splitlines()
    chunks: list[tuple[int, int, str]] = []
    current: list[str] = []
    current_chars = 0
    start = 1
    for i, line in enumerate(lines, start=1):
        rendered = f"{i}:{line}"
        if current and current_chars + len(rendered) > char_budget:
            chunks.append((start, i - 1, "\n".join(current)))
            current = []
            current_chars = 0
            start = i
        current.append(rendered)
        current_chars += len(rendered) + 1
    if current:
        chunks.append((start, len(lines), "\n".join(current)))
    return chunks


def _no_buffer() -> ToolResult:
    return ToolResult(
        tool_call_id="",
        content="Output buffering is not enabled for this session.",
        is_error=True,
    )


class BufferGrepTool:
    name = "buffer_grep"
    description = (
        "Search a stored tool-output buffer for a regular expression. Returns matching "
        "lines as line:content. Use the buffer_id from a [buffer:...] stub in the conversation."
    )
    parameters = {
        "type": "object",
        "properties": {
            "buffer_id": {"type": "string", "description": "Buffer id from a [buffer:...] stub"},
            "pattern": {"type": "string", "description": "Regular expression to search for"},
            "max_results": {
                "type": "integer",
                "description": f"Maximum matching lines (default {DEFAULT_BUFFER_GREP_MAX})",
            },
        },
        "required": ["buffer_id", "pattern"],
    }

    async def execute(self, arguments: dict, env: Environment, ctx: ToolContext) -> ToolResult:
        buffer = getattr(ctx, "buffer", None)
        if buffer is None:
            return _no_buffer()
        buffer_id = arguments["buffer_id"]
        pattern = arguments["pattern"]
        max_results = arguments.get("max_results") or DEFAULT_BUFFER_GREP_MAX
        try:
            matches = buffer.grep(buffer_id, pattern, max_results=max_results)
        except KeyError as exc:
            return ToolResult(tool_call_id="", content=str(exc), is_error=True)
        except Exception as exc:  # noqa: BLE001 - bad regex, etc.
            return ToolResult(
                tool_call_id="", content=f"buffer_grep failed: {type(exc).__name__}: {exc}", is_error=True
            )
        if not matches:
            return ToolResult(tool_call_id="", content=f"No matches for {pattern} in buffer {buffer_id}")
        capped = len(matches) >= max_results
        out = "\n".join(matches)
        if capped:
            out += f"\n(results capped at {max_results})"
        return ToolResult(tool_call_id="", content=out)


class BufferSliceTool:
    name = "buffer_slice"
    description = (
        "Read a line range from a stored tool-output buffer. Returns lines as line:content. "
        "Use the buffer_id from a [buffer:...] stub in the conversation."
    )
    parameters = {
        "type": "object",
        "properties": {
            "buffer_id": {"type": "string", "description": "Buffer id from a [buffer:...] stub"},
            "start_line": {"type": "integer", "description": "First line (1-based, inclusive)"},
            "end_line": {"type": "integer", "description": "Last line (1-based, inclusive)"},
        },
        "required": ["buffer_id", "start_line", "end_line"],
    }

    async def execute(self, arguments: dict, env: Environment, ctx: ToolContext) -> ToolResult:
        buffer = getattr(ctx, "buffer", None)
        if buffer is None:
            return _no_buffer()
        try:
            content = buffer.slice(
                arguments["buffer_id"], int(arguments["start_line"]), int(arguments["end_line"])
            )
        except KeyError as exc:
            return ToolResult(tool_call_id="", content=str(exc), is_error=True)
        return ToolResult(tool_call_id="", content=content or "(no lines in range)")


class BufferQueryTool:
    name = "buffer_query"
    description = (
        "Ask a natural-language question about a large stored tool-output buffer. A helper model "
        "scans the buffer in chunks and returns the relevant lines (with line numbers) plus a "
        "short answer. Use when you don't know an exact pattern to grep for. This makes extra "
        "model calls, so prefer buffer_grep / buffer_slice when you know what to look for."
    )
    parameters = {
        "type": "object",
        "properties": {
            "buffer_id": {"type": "string", "description": "Buffer id from a [buffer:...] stub"},
            "question": {"type": "string", "description": "What you want to find in the buffer"},
            "max_chunks": {
                "type": "integer",
                "description": f"Max chunks to scan (default {DEFAULT_QUERY_MAX_CHUNKS}); caps cost",
            },
        },
        "required": ["buffer_id", "question"],
    }

    async def execute(self, arguments: dict, env: Environment, ctx: ToolContext) -> ToolResult:
        buffer = getattr(ctx, "buffer", None)
        if buffer is None:
            return _no_buffer()
        model = getattr(ctx, "model", None)
        if model is None:
            return ToolResult(
                tool_call_id="",
                content="buffer_query needs a model but none is available; use buffer_grep / buffer_slice.",
                is_error=True,
            )
        buffer_id = arguments["buffer_id"]
        question = arguments["question"]
        max_chunks = int(arguments.get("max_chunks") or DEFAULT_QUERY_MAX_CHUNKS)
        try:
            text = buffer.read(buffer_id)
        except KeyError as exc:
            return ToolResult(tool_call_id="", content=str(exc), is_error=True)

        chunks = _chunk_lines(text, QUERY_CHUNK_CHARS)
        truncated = len(chunks) > max_chunks
        chunks = chunks[:max_chunks]

        excerpts: list[str] = []
        total = 0
        for _start, _end, body in chunks:
            try:
                resp = await model.complete(
                    [
                        Message(role=Role.SYSTEM, content=_MAP_SYSTEM),
                        Message(
                            role=Role.USER,
                            content=f"Question: {question}\n\nExcerpt:\n{body}",
                        ),
                    ]
                )
            except Exception:  # noqa: BLE001 - one bad chunk shouldn't fail the whole query
                continue
            out = (resp.content or "").strip()
            if not out or out.upper() == "NONE":
                continue
            excerpts.append(out)
            total += len(out)
            if total >= QUERY_MAX_EXCERPT_CHARS:
                truncated = True
                break

        if not excerpts:
            note = " (scan was capped — try buffer_grep)" if truncated else ""
            return ToolResult(
                tool_call_id="", content=f"No relevant lines found for: {question}{note}"
            )

        joined = "\n".join(excerpts)[:QUERY_MAX_EXCERPT_CHARS]
        answer = ""
        try:
            resp = await model.complete(
                [
                    Message(role=Role.SYSTEM, content=_REDUCE_SYSTEM),
                    Message(
                        role=Role.USER,
                        content=f"Question: {question}\n\nExcerpt lines:\n{joined}",
                    ),
                ]
            )
            answer = (resp.content or "").strip()
        except Exception:  # noqa: BLE001 - reduce is best-effort; excerpts still returned
            answer = ""

        parts: list[str] = []
        if answer:
            parts.append(f"Answer: {answer}")
        parts.append("Relevant lines:\n" + joined)
        if truncated:
            parts.append("(note: buffer scan was capped; run buffer_grep for an exhaustive search)")
        return ToolResult(tool_call_id="", content="\n\n".join(parts))


class BufferListTool:
    name = "buffer_list"
    description = "List the stored tool-output buffers for this session (id, size, lines, source tool)."
    parameters = {"type": "object", "properties": {}, "required": []}

    async def execute(self, arguments: dict, env: Environment, ctx: ToolContext) -> ToolResult:
        buffer = getattr(ctx, "buffer", None)
        if buffer is None:
            return _no_buffer()
        refs = buffer.list_buffers()
        if not refs:
            return ToolResult(tool_call_id="", content="No buffers stored for this session.")
        lines = [
            f"{r.buffer_id} | {r.size_bytes} bytes | {r.line_count} lines"
            + (f" | tool={r.tool_name}" if r.tool_name else "")
            for r in refs
        ]
        return ToolResult(tool_call_id="", content="\n".join(lines))
