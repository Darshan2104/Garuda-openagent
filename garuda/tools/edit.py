import re
from dataclasses import dataclass

from garuda.tools.protocol import ToolContext
from garuda.types import ToolResult
from garuda.workspace.protocol import Environment

_SNIPPET_CONTEXT_LINES = 2

# A read_file line-number prefix: leading spaces, digits, then a TAB — matches
# ReadFileTool's `f"{i:>{width}}\t{line}"` rendering. Models sometimes paste these
# into old_string verbatim; stripping them recovers the real text.
_LINE_NO_PREFIX_RE = re.compile(r"^[ \t]*\d+\t")


def _normalize_ws(text: str) -> str:
    """Collapse each line to its stripped form for whitespace-insensitive comparison."""
    return "\n".join(line.strip() for line in text.splitlines())


def _normalize_eol(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _leading_ws(line: str) -> str:
    return line[: len(line) - len(line.lstrip())]


def _dominant_eol(text: str) -> str:
    if "\r\n" in text:
        return "\r\n"
    if "\r" in text:
        return "\r"
    return "\n"


def _strip_line_number_prefixes(text: str) -> str:
    return "\n".join(_LINE_NO_PREFIX_RE.sub("", ln) for ln in text.split("\n"))


def _no_match_hint(content: str, old_string: str) -> str:
    """Explain a likely reason ``old_string`` was not found, when we can spot one.

    The dominant edit failure is a copy that differs only in indentation/whitespace
    or line endings; a bare 'not found' sends the model in circles. Point it at the
    real fix instead.
    """
    if _normalize_eol(old_string) in _normalize_eol(content):
        return " A match exists but line endings differ (the file may use CRLF); read the file and copy its exact bytes."
    normalized = _normalize_ws(old_string)
    if normalized and normalized in _normalize_ws(content):
        return (
            " A near-match exists that differs only in leading/trailing whitespace or indentation. "
            "Re-read the file and copy old_string exactly, including its indentation."
        )
    return " Re-read the file to copy the exact text (whitespace included); it may have changed or never matched."


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


def _first_diff(before: str, after: str) -> int:
    """Character offset where two strings first differ (len of the shorter if one
    is a prefix of the other). Locates the edit site even when the replacement was
    transformed (re-indented / prefix-stripped) and so isn't found verbatim."""
    limit = min(len(before), len(after))
    for i in range(limit):
        if before[i] != after[i]:
            return i
    return limit


def _ambiguous_error(count: int, path: str) -> str:
    return (
        f"old_string occurs {count} times in {path}. "
        "Add more surrounding context to make it unique, "
        "or set replace_all to true to replace every occurrence."
    )


@dataclass
class EditOutcome:
    """Result of resolving an edit against a file's contents.

    ``updated`` is the new full-file content on success (``None`` on failure);
    ``note`` is non-empty only when a recovery layer (not an exact match) matched,
    so the caller can flag it for the model to verify.
    """

    updated: str | None
    replacements: int
    note: str
    error: str | None

    @property
    def ok(self) -> bool:
        return self.error is None and self.updated is not None


def _apply_literal(
    content: str, needle: str, new_string: str, path: str, replace_all: bool, note: str
) -> EditOutcome | None:
    """Literal substring replacement. Returns an EditOutcome (success, or an
    ambiguous-match error) when ``needle`` is present, else ``None`` so the caller
    falls through to the next recovery layer."""
    count = content.count(needle)
    if count == 0:
        return None
    if count > 1 and not replace_all:
        return EditOutcome(None, 0, "", _ambiguous_error(count, path))
    if replace_all:
        return EditOutcome(content.replace(needle, new_string), count, note, None)
    return EditOutcome(content.replace(needle, new_string, 1), 1, note, None)


def _apply_eol_normalized(
    content: str, old_string: str, new_string: str, path: str, replace_all: bool
) -> EditOutcome | None:
    """Recover when old_string matches only after line-ending normalization (the
    file uses CRLF/CR but the model typed LF, or vice versa)."""
    norm_content = _normalize_eol(content)
    norm_old = _normalize_eol(old_string)
    # Nothing to gain if neither side carried a CR — Layer 1 already covered it.
    if norm_content == content and norm_old == old_string:
        return None
    count = norm_content.count(norm_old)
    if count == 0:
        return None
    if count > 1 and not replace_all:
        return EditOutcome(None, 0, "", _ambiguous_error(count, path))
    eol = _dominant_eol(content)
    old_native = norm_old.replace("\n", eol)
    new_native = _normalize_eol(new_string).replace("\n", eol)
    if replace_all:
        reps = content.count(old_native)
        updated = content.replace(old_native, new_native)
    else:
        reps = 1
        updated = content.replace(old_native, new_native, 1)
    # Mixed line endings: the native needle may not appear in the raw bytes. Bail
    # rather than silently write nothing / the wrong thing.
    if reps == 0 or updated == content:
        return None
    return EditOutcome(updated, reps, "matched after normalizing line endings", None)


def _reindent(new_lines: list[str], model_indent: str, file_indent: str) -> list[str] | None:
    """Shift ``new_lines`` from the model's indentation to the file's actual one.

    Returns ``None`` when the shift can't be done safely (a tab/space mismatch),
    so the caller falls back to a clear error instead of writing inconsistent
    indentation.
    """
    if model_indent == file_indent:
        return list(new_lines)
    # Only shift when both indents are spaces-only; mixing tabs makes a column
    # delta meaningless (and a tab-vs-space file should be copied exactly).
    if "\t" in model_indent or "\t" in file_indent:
        return None
    delta = len(file_indent) - len(model_indent)
    shifted: list[str] = []
    for line in new_lines:
        if not line.strip():
            shifted.append(line)  # leave blank lines alone
        elif delta >= 0:
            shifted.append(" " * delta + line)
        else:
            removable = len(line) - len(line.lstrip(" "))
            shifted.append(line[min(removable, -delta):])
    return shifted


def _apply_whitespace_flexible(
    content: str, old_string: str, new_string: str, path: str, replace_all: bool
) -> EditOutcome | None:
    """Recover when old_string matches ignoring per-line indentation/whitespace,
    re-anchoring the replacement to the file's real indentation. Unique-match only
    (never guesses); bails on tab/space mismatches it can't safely re-indent."""
    eol = _dominant_eol(content)
    content_lines = _normalize_eol(content).split("\n")
    old_lines = _normalize_eol(old_string).split("\n")
    new_lines = _normalize_eol(new_string).split("\n")
    stripped_old = [ln.strip() for ln in old_lines]
    if not any(stripped_old):  # an all-blank old_string can't be anchored
        return None
    window = len(old_lines)
    starts = [
        i
        for i in range(0, len(content_lines) - window + 1)
        if [content_lines[j].strip() for j in range(i, i + window)] == stripped_old
    ]
    if not starts:
        return None
    if len(starts) > 1 and not replace_all:
        return EditOutcome(
            None,
            0,
            "",
            f"a whitespace-insensitive match for old_string occurs {len(starts)} times in "
            f"{path}; add more surrounding context, or set replace_all to true.",
        )
    targets = starts if replace_all else starts[:1]
    result_lines = list(content_lines)
    model_indent = _leading_ws(old_lines[0])
    for start in sorted(targets, reverse=True):
        file_indent = _leading_ws(content_lines[start])
        reindented = _reindent(new_lines, model_indent, file_indent)
        if reindented is None:
            return None  # unsafe to re-anchor; fall through to a clear error
        result_lines[start : start + window] = reindented
    return EditOutcome(
        eol.join(result_lines),
        len(targets),
        "matched ignoring indentation; new text re-indented to the file",
        None,
    )


def resolve_edit(
    content: str, old_string: str, new_string: str, path: str, replace_all: bool
) -> EditOutcome:
    """Resolve an edit through layered matching, most-exact first.

    Each layer only accepts a **unique** match (or all matches under replace_all)
    and never guesses on ambiguity — so a recovery can save a wasted retry, but can
    never silently apply the wrong edit. Layers:

    1. exact literal (unchanged fast path)
    2. strip pasted read_file line-number prefixes from old/new
    3. line-ending (CRLF/LF) normalization
    4. per-line whitespace/indentation flexibility, re-indented to the file
    """
    outcome = _apply_literal(content, old_string, new_string, path, replace_all, note="")
    if outcome is not None:
        return outcome

    stripped_old = _strip_line_number_prefixes(old_string)
    if stripped_old != old_string and stripped_old.strip():
        # If the model pasted line numbers into old_string it almost certainly did
        # so in new_string too; strip both so we never write a "NNN<tab>" prefix.
        stripped_new = _strip_line_number_prefixes(new_string)
        outcome = _apply_literal(
            content,
            stripped_old,
            stripped_new,
            path,
            replace_all,
            note="matched after stripping read_file line-number prefixes from old_string",
        )
        if outcome is not None:
            return outcome

    outcome = _apply_eol_normalized(content, old_string, new_string, path, replace_all)
    if outcome is not None:
        return outcome

    outcome = _apply_whitespace_flexible(content, old_string, new_string, path, replace_all)
    if outcome is not None:
        return outcome

    return EditOutcome(
        None, 0, "", f"old_string not found in {path}.{_no_match_hint(content, old_string)}"
    )


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

        if old_string == "":
            return ToolResult(
                tool_call_id="",
                content=(
                    "old_string must be non-empty. To create a file or replace its whole "
                    "contents, use the write_file tool instead."
                ),
                is_error=True,
            )

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

        outcome = resolve_edit(content, old_string, new_string, path, replace_all)
        if not outcome.ok:
            return ToolResult(tool_call_id="", content=outcome.error, is_error=True)

        await env.write_file(path, outcome.updated)

        snippet = _snippet_around(outcome.updated, _first_diff(content, outcome.updated))
        plural = "s" if outcome.replacements != 1 else ""
        message = f"Edited {path} ({outcome.replacements} replacement{plural})"
        if outcome.note:
            message += f"\n({outcome.note} — verify the snippet below)"
        if snippet:
            message += f"\n\nSnippet of new content:\n{snippet}"
        from garuda.tools.diagnostics import post_edit_report

        message += await post_edit_report(env, path, ctx)
        return ToolResult(tool_call_id="", content=message)
