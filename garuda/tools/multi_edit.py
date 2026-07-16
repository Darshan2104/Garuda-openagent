"""Multi-hunk, single-file atomic edit (Feature 2).

`multi_edit` applies a sequence of find/replace edits to ONE file in a single call.
Edits are applied in order to an in-memory copy — so edit *k* sees the result of
edit *k-1* — and the file is written only if **every** edit matches. Any failure
leaves the file untouched and reports which edit failed, so the model fixes one
hunk instead of a half-applied file. It reuses the same layered matcher as `edit`
(`resolve_edit`), so line-number-prefix / CRLF / indentation recovery applies here
too.
"""

from garuda.tools.edit import _first_diff, _snippet_around, resolve_edit
from garuda.tools.protocol import ToolContext
from garuda.types import ToolResult
from garuda.workspace.protocol import Environment


class MultiEditTool:
    name = "multi_edit"
    description = (
        "Apply several exact string replacements to a SINGLE file in one call. "
        "Edits apply in order (each sees the previous edit's result) and atomically: "
        "if any old_string does not match, nothing is written and the failing edit is "
        "reported. Prefer this over multiple `edit` calls when one file needs several "
        "changes. Each edit's old_string must match exactly and, unless replace_all is "
        "true, be unique. To create a new file, use write_file instead."
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "File path relative to workspace or absolute",
            },
            "edits": {
                "type": "array",
                "description": "Ordered list of edits to apply to the file",
                "items": {
                    "type": "object",
                    "properties": {
                        "old_string": {
                            "type": "string",
                            "description": "Exact text to replace (must exist after prior edits)",
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
                    "required": ["old_string", "new_string"],
                },
            },
        },
        "required": ["path", "edits"],
    }

    async def execute(
        self,
        arguments: dict,
        env: Environment,
        ctx: ToolContext,
    ) -> ToolResult:
        path = arguments["path"]
        edits = arguments.get("edits")
        if not isinstance(edits, list) or not edits:
            return ToolResult(
                tool_call_id="",
                content="edits must be a non-empty list of {old_string, new_string} objects.",
                is_error=True,
            )

        try:
            content = await env.read_file(path)
        except (FileNotFoundError, IsADirectoryError, OSError) as exc:
            return ToolResult(
                tool_call_id="",
                content=(
                    f"Cannot edit {path}: file could not be read ({exc}). "
                    "To create a new file, use the write_file tool instead."
                ),
                is_error=True,
            )

        working = content
        total_replacements = 0
        notes: list[str] = []
        n = len(edits)
        for index, edit in enumerate(edits, start=1):
            error = self._validate(edit, index)
            if error:
                return self._abort(error)
            outcome = resolve_edit(
                working,
                edit["old_string"],
                edit["new_string"],
                path,
                bool(edit.get("replace_all", False)),
            )
            if not outcome.ok:
                return self._abort(f"edit #{index} of {n} failed: {outcome.error}")
            working = outcome.updated
            total_replacements += outcome.replacements
            if outcome.note:
                notes.append(f"edit #{index}: {outcome.note}")

        await env.write_file(path, working)

        snippet = _snippet_around(working, _first_diff(content, working))
        message = f"Applied {n} edit{'s' if n != 1 else ''} to {path} ({total_replacements} replacement(s) total)"
        for note in notes:
            message += f"\n({note} — verify the snippet below)"
        if snippet:
            message += f"\n\nSnippet near first change:\n{snippet}"
        from garuda.tools.diagnostics import post_edit_report

        message += await post_edit_report(env, path, ctx)
        return ToolResult(tool_call_id="", content=message)

    @staticmethod
    def _validate(edit: object, index: int) -> str | None:
        if not isinstance(edit, dict):
            return f"edit #{index} must be an object with old_string and new_string."
        old = edit.get("old_string")
        new = edit.get("new_string")
        if old is None or new is None:
            return f"edit #{index} needs both old_string and new_string."
        if old == "":
            return (
                f"edit #{index}: old_string must be non-empty. Use write_file to create a "
                "file or replace its whole contents."
            )
        if old == new:
            return f"edit #{index}: old_string and new_string are identical — nothing to change."
        return None

    @staticmethod
    def _abort(reason: str) -> ToolResult:
        return ToolResult(
            tool_call_id="",
            content=(
                f"{reason}\n\nNo changes were written (multi_edit is all-or-nothing). "
                "Fix the failing edit and resubmit the full edits list."
            ),
            is_error=True,
        )
