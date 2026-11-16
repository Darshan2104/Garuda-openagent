"""Bounded, run-owned WorkspaceEdit previews and Environment-backed application."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import secrets
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from garuda.lsp.positions import from_utf16, lsp_lines, to_utf16, uri_to_path
from garuda.lsp.protocol import LanguageService, LanguageServiceError, WorkspaceEditResponse
from garuda.types import ToolResult
from garuda.workspace.paths import resolve_workspace_path
from garuda.workspace.protocol import WorkspaceAccessDenied
from garuda.workspace.settlement import settle_task

MAX_FILES = 32
MAX_EDITS = 256
MAX_TEXT_BYTES = 4 * 1024 * 1024
MAX_PREVIEW_BYTES = 12_000
MAX_PREVIEWS = 4


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def workspace_path(root: str, path: str) -> str:
    logical = resolve_workspace_path(Path(root), path, True)
    physical = logical.resolve(strict=True)
    if not physical.is_relative_to(Path(root).resolve()):
        raise WorkspaceAccessDenied(f"Path {path} resolves outside the workspace")
    if physical != logical:
        raise LanguageServiceError(f"Symlink aliases are unsupported in WorkspaceEdits: {path}")
    if not physical.is_file():
        raise LanguageServiceError(f"Not an existing file: {path}")
    return str(physical)


def edit_documents(edit) -> list[tuple[str, int | None, list]]:
    """Validate the complete edit; never silently drop a malformed operation."""
    if not isinstance(edit, dict) or not edit:
        raise LanguageServiceError("No WorkspaceEdit returned")
    if set(edit) - {"changes", "documentChanges", "changeAnnotations"}:
        raise LanguageServiceError("Unsupported WorkspaceEdit fields")
    if "changes" in edit and "documentChanges" in edit:
        raise LanguageServiceError("Ambiguous WorkspaceEdit: both changes and documentChanges")
    documents = []
    if "changes" in edit:
        if not isinstance(edit["changes"], dict):
            raise LanguageServiceError("Invalid WorkspaceEdit changes")
        documents = [(uri, None, edits) for uri, edits in edit["changes"].items()]
    elif "documentChanges" in edit:
        changes = edit["documentChanges"]
        if not isinstance(changes, list):
            raise LanguageServiceError("Invalid documentChanges")
        for change in changes:
            if not isinstance(change, dict) or "kind" in change:
                raise LanguageServiceError("Unsupported resource operation (create/rename/delete)")
            doc = change.get("textDocument")
            if (set(change) != {"textDocument", "edits"} or not isinstance(doc, dict)
                    or set(doc) != {"uri", "version"}):
                raise LanguageServiceError("Invalid TextDocumentEdit")
            version = doc["version"]
            if version is not None and (type(version) is not int or version < 0):
                raise LanguageServiceError("Invalid document version")
            documents.append((doc["uri"], version, change["edits"]))
    if not documents or len(documents) > MAX_FILES:
        raise LanguageServiceError(f"WorkspaceEdit must touch 1–{MAX_FILES} files")
    count = 0
    for uri, _, edits in documents:
        if (not isinstance(uri, str) or uri_to_path(uri) is None
                or urlsplit(uri).query or urlsplit(uri).fragment):
            raise LanguageServiceError(f"Unsupported file URI: {uri!r}")
        if not isinstance(edits, list) or not edits:
            raise LanguageServiceError("Text edits must be a non-empty list")
        count += len(edits)
    if count > MAX_EDITS:
        raise LanguageServiceError(f"WorkspaceEdit exceeds {MAX_EDITS} edits")
    annotations = edit.get("changeAnnotations", {})
    if (not isinstance(annotations, dict) or any(
            not isinstance(a, dict) or a.get("needsConfirmation") is not False
            for a in annotations.values())):
        raise LanguageServiceError("Unsupported change annotations requiring confirmation")
    return documents


def _offset(text: str, position: dict) -> tuple[int, int, int]:
    if not isinstance(position, dict) or set(position) != {"line", "character"}:
        raise LanguageServiceError("Invalid edit position")
    line, character = position["line"], position["character"]
    if type(line) is not int or type(character) is not int or min(line, character) < 0:
        raise LanguageServiceError("Invalid edit position")
    lines = lsp_lines(text)
    if line >= len(lines):
        raise LanguageServiceError("Edit line is outside the document")
    column = from_utf16(lines[line], character)
    if to_utf16(lines[line], column) != character:
        raise LanguageServiceError("Edit UTF-16 offset is outside the line or splits a surrogate pair")
    starts = [0] + [m.end() for m in re.finditer(r"\r\n|\r|\n", text)]
    return starts[line] + column, line + 1, column + 1


@dataclass(frozen=True)
class FileEdit:
    path: str
    original: str
    updated: str
    version: int
    before_digest: str
    edits: tuple[dict, ...]


@dataclass(frozen=True)
class Preview:
    root: str
    session: str
    files: tuple[FileEdit, ...]
    service: LanguageService


def _result(files: list[dict], error: str | None = None) -> ToolResult:
    return ToolResult(tool_call_id="", is_error=error is not None,
                      content=json.dumps({"error": error, "files": files}, ensure_ascii=False),
                      metadata={"files": files})


def preview_limit(ctx) -> int:
    limit = MAX_PREVIEW_BYTES
    if callable(ctx.output_budget):
        budget = ctx.output_budget(False)
        if type(budget) is not int or budget <= 0:
            raise LanguageServiceError("Preview output budget is unavailable")
        limit = min(limit, budget)
    if ctx.buffer is not None:
        limit = min(limit, ctx.buffer.threshold_bytes)
    return limit


class WorkspaceEdits:
    def __init__(self):
        self._previews: OrderedDict[str, Preview] = OrderedDict()
        self._lock = asyncio.Lock()

    async def close(self) -> None:
        self._previews.clear()

    async def preview(self, request, service, env, ctx, root: str,
                      source_path: str, source_text: str) -> ToolResult:
        # Discovery responses are discarded. Only a response against synced text
        # can become an applicable preview, including for initially closed files.
        async with self._lock:
            observed = {source_path: source_text}
            for _ in range(3):
                if (await env.read_bytes(source_path)).decode("utf-8") != source_text:
                    raise LanguageServiceError(f"Content changed during preview: {source_path}")
                response: WorkspaceEditResponse = await request()
                documents = edit_documents(response.edit)
                snapshots = {os.path.realpath(d.path): d for d in response.documents}
                resolved, seen, total = [], set(), 0
                missing = False
                for uri, version, edits in documents:
                    path = workspace_path(root, uri_to_path(uri))
                    stat = Path(path).stat()
                    identity = (stat.st_dev, stat.st_ino)
                    if identity in seen:
                        raise LanguageServiceError("Duplicate file or URI alias in WorkspaceEdit")
                    seen.add(identity)
                    if ctx.permissions is None:
                        raise LanguageServiceError("Preview requires the run's permission engine")
                    allowed, reason = await ctx.permissions.evaluate_tool_call("lsp", {"path": path})
                    if not allowed:
                        raise WorkspaceAccessDenied(f"Cannot preview {path}: {reason}")
                    text = (await env.read_bytes(path)).decode("utf-8")
                    total += len(text.encode("utf-8"))
                    if total > MAX_TEXT_BYTES:
                        raise LanguageServiceError("WorkspaceEdit source text exceeds 4 MiB")
                    snapshot = snapshots.get(path)
                    if path in observed and observed[path] != text:
                        raise LanguageServiceError(f"Content changed during preview: {path}")
                    observed[path] = text
                    if snapshot is None or snapshot.text != text:
                        await service.sync(path, text, root)
                        missing = True
                    resolved.append((path, version, edits, snapshot))
                if missing:
                    continue
                files = []
                preview_size = 0
                for path, version, edits, snapshot in resolved:
                    if version is not None and version != snapshot.version:
                        raise LanguageServiceError(f"Stale document version for {path}")
                    replacements, rows = [], []
                    for edit in edits:
                        if (not isinstance(edit, dict) or set(edit) != {"range", "newText"}
                                or not isinstance(edit["newText"], str)):
                            raise LanguageServiceError("Unsupported or malformed TextEdit")
                        rng = edit["range"]
                        if not isinstance(rng, dict) or set(rng) != {"start", "end"}:
                            raise LanguageServiceError("Invalid edit range")
                        start, sl, sc = _offset(snapshot.text, rng["start"])
                        end, el, ec = _offset(snapshot.text, rng["end"])
                        if end < start:
                            raise LanguageServiceError("Reversed edit range")
                        replacements.append((start, end, edit["newText"]))
                        rows.append({"range": [sl, sc, el, ec], "replacement": edit["newText"]})
                    replacements.sort()
                    for a, b in zip(replacements, replacements[1:], strict=False):
                        if a[1] > b[0] or a[0] == b[0]:
                            raise LanguageServiceError("Overlapping or ambiguous text edits")
                    updated = snapshot.text
                    for start, end, replacement in reversed(replacements):
                        updated = updated[:start] + replacement + updated[end:]
                    preview_size += len(updated.encode("utf-8"))
                    if preview_size > MAX_TEXT_BYTES:
                        raise LanguageServiceError("WorkspaceEdit result exceeds 4 MiB")
                    files.append(FileEdit(path, snapshot.text, updated, snapshot.version,
                                          digest(snapshot.text), tuple(rows)))
                if (await env.read_bytes(source_path)).decode("utf-8") != source_text:
                    raise LanguageServiceError(f"Content changed during preview: {source_path}")
                token = secrets.token_urlsafe(24)
                rows = [{"path": f.path, "document_version": f.version,
                         "sha256": f.before_digest, "edits": f.edits} for f in files]
                content = json.dumps({"preview_token": token, "files": rows}, ensure_ascii=False)
                if len(content.encode("utf-8")) > preview_limit(ctx):
                    raise LanguageServiceError(
                        "WorkspaceEdit exceeds the preview limit (12000 bytes or the smaller "
                        "live output allowance); no token issued")
                self._previews[token] = Preview(root, ctx.session_id, tuple(files), service)
                while len(self._previews) > MAX_PREVIEWS:
                    self._previews.popitem(last=False)
                return ToolResult(tool_call_id="", content=content,
                                  metadata={"preview_token": token, "files": rows})
        raise LanguageServiceError("WorkspaceEdit targets did not stabilize against synced documents")

    async def apply(self, token, env, ctx, root: str) -> ToolResult:
        async with self._lock:
            preview = self._previews.get(token) if isinstance(token, str) else None
            if preview is None or preview.root != root or preview.session != ctx.session_id:
                return _result([], "Unknown preview token for this run/workspace; preview first")
            rows = [{"path": f.path, "status": "not_written"} for f in preview.files]
            try:
                if ctx.permissions is None:
                    raise LanguageServiceError("Apply requires the run's permission engine")
                identities = set()
                for file in preview.files:
                    if workspace_path(root, file.path) != file.path:
                        raise LanguageServiceError(f"File identity changed: {file.path}")
                    stat = Path(file.path).stat()
                    identity = (stat.st_dev, stat.st_ino)
                    if identity in identities:
                        raise LanguageServiceError("Targets now alias the same file")
                    identities.add(identity)
                    allowed, reason = await ctx.permissions.evaluate_tool_call(
                        "lsp_apply", {"path": file.path, "preview_token": token})
                    if not allowed:
                        raise WorkspaceAccessDenied(f"Cannot apply {file.path}: {reason}")
                for file in preview.files:
                    text = (await env.read_bytes(file.path)).decode("utf-8")
                    if digest(text) != file.before_digest:
                        raise LanguageServiceError(f"Stale preview: content changed for {file.path}")
            except (OSError, UnicodeError, LanguageServiceError) as exc:
                result = _result(rows, str(exc))
                if isinstance(exc, WorkspaceAccessDenied):
                    result.metadata["permission_denied"] = True
                return result
            # Consumed before the first write, including when rollback is needed.
            del self._previews[token]
            cancelled = asyncio.Event()
            task = asyncio.create_task(self._write(preview, rows, env, cancelled))
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError as exc:
                cancelled.set()
                result = await settle_task(task)
                raise exc from LanguageServiceError(result.content)

    async def _write(self, preview, rows, env, cancelled) -> ToolResult:
        attempted = []
        try:
            for file, row in zip(preview.files, rows, strict=True):
                if cancelled.is_set():
                    raise LanguageServiceError("Apply cancelled")
                attempted.append((file, row))
                row["status"] = "write_failed"
                await env.write_file(file.path, file.updated)
                if await env.read_bytes(file.path) != file.updated.encode("utf-8"):
                    raise LanguageServiceError(f"Write verification failed for {file.path}")
                row["status"] = "applied"
                if cancelled.is_set():
                    raise LanguageServiceError("Apply cancelled")
            result = _result(rows)
            try:
                for file in preview.files:
                    await preview.service.sync(file.path, file.updated, preview.root)
            except Exception as exc:
                result.metadata["language_sync_error"] = str(exc)
                result.content += f"\nFiles applied, but language-server synchronization failed: {exc}"
            if cancelled.is_set():
                raise LanguageServiceError("Apply cancelled")
            return result
        except (Exception, asyncio.CancelledError) as exc:
            if attempted:
                attempted[-1][1]["write_error"] = str(exc)
            for file, row in reversed(attempted):
                try:
                    await env.write_file(file.path, file.original)
                    if await env.read_bytes(file.path) != file.original.encode("utf-8"):
                        raise LanguageServiceError("Restored content did not match original")
                    row["status"] = "restored"
                except Exception as rollback:
                    row.update(status="rollback_failed", rollback_error=str(rollback))
            result = _result(rows, f"Apply failed: {exc}; see per-file restoration results")
            for file, row in attempted:
                if row["status"] == "restored":
                    try:
                        await preview.service.sync(file.path, file.original, preview.root)
                    except Exception as sync_error:
                        result.metadata["language_sync_error"] = str(sync_error)
            if "language_sync_error" in result.metadata:
                result.content += ("\nLanguage-server synchronization failed: "
                                   + result.metadata["language_sync_error"])
            return result
