"""Digest-bound Python codemod previews and explicit, permission-screened apply."""

import asyncio
import difflib
import hashlib
import io
import json
import posixpath
import secrets
import tokenize
from dataclasses import dataclass, field

from garuda.core.permissions import permission_workspace
from garuda.tools.protocol import ToolContext, ToolEffect
from garuda.tools.python_rewrite import MAX_REPLACEMENTS, identifier, rename_import
from garuda.types import ToolResult
from garuda.workspace.protocol import Environment
from garuda.workspace.settlement import settle_task

MAX_FILES = 16
MAX_FILE_BYTES = 128 * 1024
MAX_PREVIEWS = 4


def _digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _diff(path: str, before: str, after: str) -> str:
    chunks = difflib.unified_diff(
        list(io.StringIO(before, newline="")), list(io.StringIO(after, newline="")),
        fromfile=path, tofile=path,
    )
    return "".join(chunk if chunk.endswith(("\n", "\r")) else
                   chunk + "\n\\ No newline at end of file\n" for chunk in chunks)


def _result(status, *, error=False, permission_denied=False, **details):
    return ToolResult(tool_call_id="", is_error=error,
                      content=json.dumps({"version": 1, "status": status, **details}),
                      metadata={"permission_denied": True} if permission_denied else {})


@dataclass(frozen=True)
class FilePreview:
    path: str
    before: bytes
    after: str
    digest: str


@dataclass(frozen=True)
class Preview:
    env: Environment
    session_id: str
    files: tuple[FilePreview, ...]


@dataclass
class CodemodState:
    previews: dict[str, Preview] = field(default_factory=dict)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class PythonCodemodTool:
    effect = ToolEffect.MUTATING
    name = "python_codemod"
    description = (
        "Preview then explicitly apply a bounded Python rename_import codemod. "
        "Preview takes paths and {kind: rename_import, module, old, new}; "
        "apply takes only the returned preview_token. Review diffs and skipped issues. "
        "Only module-level from-imports and unambiguous local name references are rewritten. "
        "Explicit aliases remain unchanged. Source is parsed on the host, never executed."
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["preview", "apply"]},
            "paths": {"type": "array", "minItems": 1, "maxItems": MAX_FILES,
                      "items": {"type": "string"}},
            "transformation": {
                "type": "object", "additionalProperties": False,
                "properties": {"kind": {"const": "rename_import"},
                               "module": {"type": "string"},
                               "old": {"type": "string"}, "new": {"type": "string"}},
                "required": ["kind", "module", "old", "new"],
            },
            "preview_token": {"type": "string"},
        },
        "required": ["action"],
        "additionalProperties": False,
    }

    async def execute(self, arguments: dict, env: Environment, ctx: ToolContext) -> ToolResult:
        action = arguments.get("action")
        expected = {"action", "paths", "transformation"} if action == "preview" else {"action", "preview_token"}
        if not isinstance(action, str) or action not in {"preview", "apply"} or set(arguments) != expected:
            return _result("refused", error=True, reason="Use preview with paths/transformation or apply with preview_token only")
        if ctx.permissions is None:
            return _result("refused", error=True, reason="Permission engine required")
        if ctx.codemod_state is None:
            ctx.codemod_state = CodemodState()
        async with ctx.codemod_state.lock:
            with permission_workspace(env.workspace_root):
                allowed, reason = await ctx.permissions.evaluate_tool_call(self.name, arguments)
                if not allowed:
                    return _result("refused", error=True, reason=reason, permission_denied=True)
                if action == "preview":
                    return await self._preview(arguments, env, ctx)
                return await self._apply(arguments["preview_token"], env, ctx)

    async def _preview(self, arguments, env, ctx):
        paths = arguments["paths"]
        transform = arguments["transformation"]
        if (not isinstance(paths, list) or not 1 <= len(paths) <= MAX_FILES or
                not all(isinstance(path, str) and path.endswith(".py") and len(path) <= 4096 and "\x00" not in path for path in paths)):
            return _result("refused", error=True, reason=f"Select 1..{MAX_FILES} distinct relative .py paths")
        normalized = [posixpath.normpath(path) for path in paths]
        if (len(set(normalized)) != len(paths) or
                any(path.startswith(("/", "../")) for path in normalized)):
            return _result("refused", error=True, reason="Duplicate or non-workspace paths")
        if (not isinstance(transform, dict) or set(transform) != {"kind", "module", "old", "new"} or
                transform["kind"] != "rename_import" or
                not isinstance(transform["module"], str) or
                not all(identifier(part) for part in transform["module"].split(".")) or
                not identifier(transform["old"]) or not identifier(transform["new"]) or
                transform["old"] == transform["new"]):
            return _result("refused", error=True, reason="Only declarative rename_import with distinct ASCII identifiers is supported")
        rows, files = [], []
        replacements = 0
        for path in normalized:
            row = {"path": path, "status": "blocked", "before_digest": None,
                   "replacements": None, "diff": "", "issues": []}
            rows.append(row)
            allowed, reason = await ctx.permissions.evaluate_tool_call("read_file", {"path": path})
            if not allowed:
                row["issues"] = [{"code": "permission_denied", "reason": reason, "blocking": True}]
                continue
            try:
                before = await env.read_bytes(path, max_bytes=MAX_FILE_BYTES)
                if len(before) > MAX_FILE_BYTES:
                    raise ValueError("Environment exceeded bounded read contract")
                row["before_digest"] = _digest(before)
                encoding, _ = tokenize.detect_encoding(io.BytesIO(before).readline)
                if encoding != "utf-8":
                    raise ValueError("Only UTF-8 source without a BOM is supported")
                source = before.decode("utf-8")
                rewrite = rename_import(source, transform["module"], transform["old"], transform["new"])
            except (OSError, ValueError, TypeError, RuntimeError, SyntaxError) as exc:
                row["issues"] = [{"code": "read_or_encoding_error", "reason": str(exc), "blocking": True}]
                continue
            row["issues"] = list(rewrite.issues)
            if rewrite.blocked:
                continue
            if len(rewrite.content.encode("utf-8")) > MAX_FILE_BYTES:
                row["issues"].append({"code": "result_size_limit", "blocking": True})
                continue
            row["replacements"] = rewrite.replacements
            replacements += rewrite.replacements
            row["status"] = "changed" if rewrite.content != source else "unchanged"
            row["diff"] = _diff(path, source, rewrite.content)
            files.append(FilePreview(path, before, rewrite.content, _digest(before)))
        blocked = any(row["status"] == "blocked" for row in rows) or replacements > MAX_REPLACEMENTS
        if blocked:
            return _result("blocked", error=True, files=rows, preview_token=None,
                           permission_denied=any(issue["code"] == "permission_denied"
                                                 for row in rows for issue in row["issues"]),
                           reason="All selected files must be safe and total replacements at most 1000")
        # Tokens select immutable host data, never model-supplied replacement text.
        identity = json.dumps({"transformation": transform, "files": [
            [file.path, file.digest, _digest(file.after.encode("utf-8"))] for file in files
        ]}, sort_keys=True)
        token = "python-codemod-v1:" + _digest((secrets.token_hex(32) + identity).encode())
        state = ctx.codemod_state
        while len(state.previews) >= MAX_PREVIEWS:
            del state.previews[next(iter(state.previews))]
        state.previews[token] = Preview(env, ctx.session_id, tuple(files))
        return _result("preview", files=rows, preview_token=token)

    async def _apply(self, token, env, ctx):
        if not isinstance(token, str):
            return _result("refused", error=True, reason="Invalid preview token")
        preview = ctx.codemod_state.previews.get(token)
        if preview is None or preview.env is not env or preview.session_id != ctx.session_id:
            return _result("refused", error=True, reason="Unknown, expired, consumed or foreign preview token")
        rows = [{"path": file.path, "status": "unattempted", "write": "unattempted",
                 "restoration": "not_needed"} for file in preview.files]
        # Finish all approval prompts before checking any content digest.
        for file, row in zip(preview.files, rows, strict=True):
            for tool in ("read_file", "write_file"):
                allowed, reason = await ctx.permissions.evaluate_tool_call(tool, {"path": file.path})
                if not allowed:
                    row.update(status="denied", reason=reason)
                    return _result("refused", error=True, files=rows, permission_denied=True)
        for file, row in zip(preview.files, rows, strict=True):
            try:
                current = await env.read_bytes(file.path, max_bytes=MAX_FILE_BYTES)
                if _digest(current) != file.digest:
                    row["status"] = "stale"
                    return _result("refused", error=True, files=rows)
            except Exception as exc:
                row.update(status="read_failed", reason=str(exc))
                return _result("refused", error=True, files=rows)
        del ctx.codemod_state.previews[token]
        # A cancellation waits for the writer to settle and rollback, rather than
        # abandoning an Environment write (local writes can be worker threads).
        interrupted = asyncio.Event()
        task = asyncio.create_task(self._write(preview.files, rows, env, interrupted, ctx.side_effects))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            interrupted.set()
            await settle_task(task)
            raise

    async def _write(self, files, rows, env, interrupted, ledger):
        attempted = []
        try:
            for file, row in zip(files, rows, strict=True):
                if interrupted.is_set():
                    raise RuntimeError("Apply cancelled")
                if file.before == file.after.encode("utf-8"):
                    row["status"] = "unchanged"
                    continue
                attempted.append((file, row))
                if ledger is not None:
                    ledger.files_written.add(file.path)
                row.update(status="write_failed", write="failed")
                await env.write_file(file.path, file.after)
                row["write"] = "completed"
                if await env.read_bytes(file.path, max_bytes=MAX_FILE_BYTES) != file.after.encode("utf-8"):
                    raise RuntimeError(f"Write verification failed: {file.path}")
                row["status"] = "applied"
            if interrupted.is_set():
                raise RuntimeError("Apply cancelled")
        except (Exception, asyncio.CancelledError) as exc:
            for file, row in reversed(attempted):
                try:
                    await env.write_file(file.path, file.before.decode("utf-8"))
                    if await env.read_bytes(file.path, max_bytes=MAX_FILE_BYTES) != file.before:
                        raise RuntimeError("Restoration verification failed")
                    row.update(status="restored", restoration="verified")
                except (Exception, asyncio.CancelledError) as restore_exc:
                    row.update(status="restoration_failed", restoration="unknown", reason=str(restore_exc) or type(restore_exc).__name__)
            restored = all(row["restoration"] == "verified" for _, row in attempted)
            return _result("rolled_back" if restored else "restoration_failed", error=True,
                           reason=str(exc) or type(exc).__name__, files=rows)
        return _result("applied", files=rows)
