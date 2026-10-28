"""Retrieval tools over the tool-output buffer (RLM-style).

When a tool's output is large it is stored in a `ToolOutputBuffer` and only a stub
enters the context. These tools let the model pull back exactly the lines it needs.
"""

import hashlib

from garuda.core.child_result import ChildResultRetrievalError, read_child_result_reference
from garuda.model import accounting
from garuda.tools.protocol import ToolContext, ToolEffect
from garuda.types import Message, Role, ToolResult
from garuda.workspace.protocol import Environment

DEFAULT_BUFFER_GREP_MAX = 100
MAX_CHARACTER_SLICE = 8 * 1024

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
    effect = ToolEffect.READ_ONLY
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
    effect = ToolEffect.READ_ONLY
    name = "buffer_slice"
    description = (
        "Read a line range, or an exact character range, from a stored tool-output buffer. "
        "Line mode returns line:content. Character mode uses zero-based start_char and an "
        "exclusive end_char and reports the next offset, which can reconstruct one long line."
    )
    parameters = {
        "type": "object",
        "properties": {
            "buffer_id": {"type": "string", "description": "Buffer id from a [buffer:...] stub"},
            "start_line": {"type": "integer", "description": "First line (1-based, inclusive)"},
            "end_line": {"type": "integer", "description": "Last line (1-based, inclusive)"},
            "start_char": {
                "type": "integer",
                "description": "First character (0-based, inclusive) for exact character mode",
            },
            "end_char": {
                "type": "integer",
                "description": "Last character (0-based, exclusive) for exact character mode",
            },
            "child_result_reference": {
                "type": "object",
                "description": (
                    "The complete reference object from a validated child-result handoff. "
                    "When supplied, the receipt is verified before any characters are returned."
                ),
            },
        },
        "required": ["buffer_id"],
    }

    async def execute(self, arguments: dict, env: Environment, ctx: ToolContext) -> ToolResult:
        result = await self._read_slice(arguments, ctx)
        if result.is_error and "child_result_reference" in arguments:
            result.metadata["buffer_slice_receipt_error"] = {
                "version": 1,
                "content_sha256": hashlib.sha256(result.content.encode("utf-8")).hexdigest(),
            }
        return result

    async def _read_slice(self, arguments: dict, ctx: ToolContext) -> ToolResult:
        buffer = getattr(ctx, "buffer", None)
        if buffer is None:
            return _no_buffer()
        try:
            char_mode = "start_char" in arguments or "end_char" in arguments
            line_mode = "start_line" in arguments or "end_line" in arguments
            if "child_result_reference" in arguments and not char_mode:
                return ToolResult(
                    tool_call_id="",
                    content="child-result retrieval failed: reference requires character mode.",
                    is_error=True,
                )
            if char_mode and line_mode:
                return ToolResult(
                    tool_call_id="",
                    content="buffer_slice accepts either a line range or a character range, not both.",
                    is_error=True,
                )
            if char_mode:
                if "start_char" not in arguments or "end_char" not in arguments:
                    return ToolResult(
                        tool_call_id="",
                        content="Character mode requires start_char and end_char.",
                        is_error=True,
                    )
                start = arguments["start_char"]
                requested_end = arguments["end_char"]
                if any(isinstance(value, bool) or not isinstance(value, int)
                       for value in (start, requested_end)):
                    return ToolResult(
                        tool_call_id="",
                        content="Character offsets must be integers (booleans are not offsets).",
                        is_error=True,
                    )
                if start < 0 or requested_end < start:
                    return ToolResult(
                        tool_call_id="",
                        content="Character offsets require 0 <= start_char <= end_char.",
                        is_error=True,
                    )
                try:
                    reference = arguments.get("child_result_reference")
                    if "child_result_reference" in arguments:
                        if not isinstance(reference, dict):
                            raise ChildResultRetrievalError("malformed child-result reference")
                        if reference.get("buffer_id") != arguments["buffer_id"]:
                            raise ChildResultRetrievalError(
                                "child-result reference does not match buffer_id"
                            )
                        _, text = read_child_result_reference(reference, buffer)
                    else:
                        text = buffer.read(arguments["buffer_id"])
                except ChildResultRetrievalError as exc:
                    content = f"child-result retrieval failed: {exc}"
                    return ToolResult(
                        tool_call_id="",
                        content=content,
                        is_error=True,
                    )
                total = len(text)
                if start > total:
                    return ToolResult(
                        tool_call_id="",
                        content=f"start_char {start} is beyond the buffer's {total} characters.",
                        is_error=True,
                    )
                if requested_end == start and start < total:
                    return ToolResult(
                        tool_call_id="",
                        content="Character range must advance while content remains.",
                        is_error=True,
                    )
                requested_end = min(requested_end, start + MAX_CHARACTER_SLICE, total)
                budget_source = getattr(ctx, "output_budget", None)
                budget = budget_source(False) if callable(budget_source) else None

                def render(end: int) -> str:
                    continuation = "complete" if end >= total else f"next_start={end}"
                    return (
                        f"[characters {start}:{end} of {total}; {continuation}]\n"
                        f"--- content ---\n{text[start:end]}"
                    )

                actual_end = requested_end
                if isinstance(budget, int):
                    low, high = start, requested_end
                    while low < high:
                        middle = (low + high + 1) // 2
                        if len(render(middle).encode("utf-8")) <= budget:
                            low = middle
                        else:
                            high = middle - 1
                    actual_end = low
                    if actual_end == start and start < total:
                        return ToolResult(
                            tool_call_id="",
                            content="Character frame cannot fit the current output budget.",
                            is_error=True,
                        )
                content = render(actual_end)
                if isinstance(budget, int) and len(content.encode("utf-8")) > budget:
                    return ToolResult(
                        tool_call_id="",
                        content="Character frame cannot fit the current output budget.",
                        is_error=True,
                    )
                return ToolResult(
                    tool_call_id="",
                    content=content,
                    metadata={"buffer_slice_frame": {
                        "version": 1,
                        "start": start,
                        "end": actual_end,
                        "total": total,
                        "complete": actual_end >= total,
                        "content_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
                    }},
                )
            if "start_line" not in arguments or "end_line" not in arguments:
                return ToolResult(
                    tool_call_id="",
                    content="Line mode requires start_line and end_line.",
                    is_error=True,
                )
            content = buffer.slice(
                arguments["buffer_id"], int(arguments["start_line"]), int(arguments["end_line"])
            )
        except KeyError as exc:
            return ToolResult(tool_call_id="", content=str(exc), is_error=True)
        return ToolResult(tool_call_id="", content=content or "(no lines in range)")


class BufferQueryTool:
    effect = ToolEffect.READ_ONLY
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
                resp = await accounting.tagged(model, "buffer_query").complete(
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
            resp = await accounting.tagged(model, "buffer_query").complete(
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
    effect = ToolEffect.READ_ONLY
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
