"""``lsp``: semantic navigation through the run's trusted language server(s).

Present only when the user's global settings configure a language server (see
:mod:`garuda.lsp.config`). The tool reads the file through the workspace
environment (so workspace path policy applies), sends that exact text to the
server, and converts positions both ways: the model speaks 1-based lines and
columns in characters, as ``read_file`` shows them; the server speaks 0-based
UTF-16. Result locations the run's path rules deny are withheld.

The effect is declared read-only because Garuda sends only read-labelled
requests and refuses server-requested edits. That label is not proof: the
server is a trusted local process that may write caches or index files and is
not confined to the workspace. In this slice it runs only for local
workspaces; Docker, remote, tmux and OS-sandbox workspaces are refused rather
than served by a host process that would bypass their boundary.
"""

from __future__ import annotations

import os
from pathlib import Path

from garuda.lsp.positions import from_utf16, lsp_lines, to_utf16, uri_to_path
from garuda.lsp.protocol import LanguageService, LanguageServiceError, Location, ServiceState
from garuda.tools.protocol import ToolContext, ToolEffect
from garuda.types import ToolResult
from garuda.workspace.paths import resolve_workspace_path
from garuda.workspace.protocol import Environment, WorkspaceAccessDenied

ACTIONS = ("definition", "references", "hover", "symbols", "diagnostics", "status")
_POSITION_ACTIONS = {"definition", "references", "hover"}
MAX_SHOWN_LOCATIONS = 100
MAX_SHOWN_SYMBOLS = 300
MAX_SHOWN_DIAGNOSTICS = 100
MAX_HOVER_SHOWN = 4000
# Distinct other files read to convert result columns, per call.
MAX_CONVERTED_FILES = 50
DIAGNOSTIC_WAIT = 5.0

_SYMBOL_KINDS = (
    "", "file", "module", "namespace", "package", "class", "method", "property", "field",
    "constructor", "enum", "interface", "function", "variable", "constant", "string", "number",
    "boolean", "array", "object", "key", "null", "enum member", "struct", "event", "operator",
    "type parameter",
)
_SEVERITIES = {1: "error", 2: "warning", 3: "info", 4: "hint"}


def _local_root(env: Environment) -> str | None:
    """Workspace root when ``env`` is a plain local workspace, else ``None``."""
    from garuda.workspace.health import HealthMonitoredEnvironment
    from garuda.workspace.local import LocalEnvironment

    while isinstance(env, HealthMonitoredEnvironment):
        env = env.inner
    # Exact type: a wrapper or subclass may change where commands run.
    if type(env) is not LocalEnvironment:
        return None
    return env.workspace_root


def _error(message: str, **metadata) -> ToolResult:
    return ToolResult(tool_call_id="", content=message, is_error=True, metadata=metadata)


class LanguageServerTool:
    effect = ToolEffect.READ_ONLY
    name = "lsp"
    parameters = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": list(ACTIONS)},
            "path": {"type": "string", "description": "File path relative to workspace or absolute"},
            "line": {"type": "integer", "description": "1-based line (definition/references/hover)"},
            "column": {
                "type": "integer",
                "description": "1-based column in characters, as read_file shows the line",
            },
            "include_declaration": {
                "type": "boolean",
                "description": "references: include the declaration itself (default true)",
            },
        },
        "required": ["action"],
    }

    def __init__(self, services: list[LanguageService]):
        self._services = list(services)
        extensions = sorted({f".{e}" for s in self._services for e in s.extensions})
        self.description = (
            "Semantic code navigation from the configured language server "
            f"({', '.join(extensions)} files): definition, references, hover, symbols "
            "(document outline) and diagnostics for a file; status shows each server's "
            "state without starting it. Prefer this over text search to follow aliases, "
            "re-exports and inheritance. Diagnostics say whether they are current for the "
            "file's present text or stale."
        )

    def _service_for(self, path: str) -> LanguageService | None:
        name = path.rsplit("/", 1)[-1]
        ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        return next((s for s in self._services if ext in s.extensions), None)

    def _status(self) -> ToolResult:
        lines = []
        for service in self._services:
            st = service.status()
            line = f"{st.name} [{st.state.value}] {', '.join('.' + e for e in st.extensions)}: {st.command}"
            if st.state is ServiceState.READY:
                line += f"\n  supported: {', '.join(st.supported) or 'none'}"
                line += f"; unsupported: {', '.join(st.unsupported) or 'none'}; diagnostics: push"
            elif st.state is ServiceState.CONFIGURED:
                line += "\n  not started; capabilities unknown until first use"
            if st.detail:
                line += f"\n  {st.detail}"
            lines.append(line)
        return ToolResult(tool_call_id="", content="\n".join(lines) or "No language servers.")

    async def execute(self, arguments: dict, env: Environment, ctx: ToolContext) -> ToolResult:
        action = arguments.get("action")
        if action not in ACTIONS:
            return _error(f"action must be one of: {', '.join(ACTIONS)}")
        if action == "status":
            return self._status()
        path = arguments.get("path")
        if not isinstance(path, str) or not path:
            return _error("path is required")
        service = self._service_for(path)
        if service is None:
            known = sorted({f".{e}" for s in self._services for e in s.extensions})
            return _error(f"No language server is configured for {path} (configured: {', '.join(known)}).")
        root = _local_root(env)
        if root is None:
            return _error(
                "The language service runs only in a local workspace in this release; "
                f"this run uses {type(getattr(env, 'inner', env)).__name__}.",
                language_server=service.name,
            )
        try:
            absolute = str(resolve_workspace_path(Path(root), path, True))
            text = await env.read_file(path)
        except (OSError, UnicodeError) as exc:
            metadata = {"permission_denied": True} if isinstance(exc, WorkspaceAccessDenied) else {}
            return _error(f"Cannot read {path}: {exc}", **metadata)
        lines = lsp_lines(text)
        position = None
        if action in _POSITION_ACTIONS:
            line, column = arguments.get("line"), arguments.get("column")
            for label, value in (("line", line), ("column", column)):
                if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                    return _error(f"{label} must be a positive integer (1-based)")
            if line > len(lines):
                return _error(f"line {line} is beyond the end of {path} ({len(lines)} lines)")
            if column > len(lines[line - 1]) + 1:
                return _error(f"column {column} is beyond the end of line {line} "
                              f"({len(lines[line - 1])} characters)")
            position = (line - 1, to_utf16(lines[line - 1], column - 1))
        metadata = {"language_server": service.name}
        try:
            version = await service.sync(absolute, text, root)
            metadata["document_version"] = version
            if action == "definition":
                found = await service.definition(absolute, *position)
                return await self._locations("definition", found, absolute, lines, env, ctx, root, metadata)
            if action == "references":
                include = arguments.get("include_declaration", True) is not False
                found = await service.references(absolute, *position, include)
                return await self._locations("reference", found, absolute, lines, env, ctx, root, metadata)
            if action == "hover":
                hover = await service.hover(absolute, *position)
                if not hover:
                    return ToolResult(tool_call_id="", content="No hover information.", metadata=metadata)
                if len(hover) > MAX_HOVER_SHOWN:
                    hover = hover[:MAX_HOVER_SHOWN] + "\n… (hover truncated)"
                return ToolResult(tool_call_id="", content=hover, metadata=metadata)
            if action == "symbols":
                return self._symbols(await service.symbols(absolute), lines, metadata)
            return self._diagnostics(await service.diagnostics(absolute, DIAGNOSTIC_WAIT),
                                     path, lines, metadata)
        except LanguageServiceError as exc:
            return _error(str(exc), **metadata)

    async def _locations(self, noun, found: list[Location], absolute, lines, env, ctx, root,
                         metadata) -> ToolResult:
        texts: dict[str, list[str] | None] = {os.path.realpath(absolute): lines}
        shown: list[str] = []
        withheld = 0
        for location in found:
            path = uri_to_path(location.uri)
            if path is None:
                shown.append(f"{location.uri} (not a local file)")
                continue
            permissions = getattr(ctx, "permissions", None)
            if permissions is not None and permissions.check_path(path, "read") == "deny":
                withheld += 1
                continue
            if len(shown) >= MAX_SHOWN_LOCATIONS:
                continue
            shown.append(await self._render(path, location, env, root, texts))
        total = len(found) - withheld
        if not found:
            return ToolResult(tool_call_id="", content=f"No {noun} found.", metadata=metadata)
        body = "\n".join(shown[:MAX_SHOWN_LOCATIONS])
        if total > MAX_SHOWN_LOCATIONS:
            body += f"\n… (+{total - MAX_SHOWN_LOCATIONS} more)"
        if withheld:
            body += f"\n({withheld} location(s) withheld by path rules)"
        metadata["locations"] = total
        return ToolResult(tool_call_id="", content=body.strip() or f"No {noun} found.",
                          metadata=metadata)

    async def _render(self, path, location: Location, env, root, texts) -> str:
        line = location.range.start_line
        display = _display_path(path, root)
        key = os.path.realpath(path)
        if key not in texts and len(texts) <= MAX_CONVERTED_FILES:
            try:
                texts[key] = lsp_lines(await env.read_file(path))
            except (OSError, UnicodeError):
                texts[key] = None
        target = texts.get(key)
        if target is None or line >= len(target):
            return (f"{display}:{line + 1} (UTF-16 offset {location.range.start_character}; "
                    "column not converted: file not readable here)")
        column = from_utf16(target[line], location.range.start_character) + 1
        return f"{display}:{line + 1}:{column}: {target[line].strip()[:200]}"

    def _symbols(self, symbols, lines, metadata) -> ToolResult:
        if not symbols:
            return ToolResult(tool_call_id="", content="No symbols.", metadata=metadata)
        rendered = []
        for symbol in symbols[:MAX_SHOWN_SYMBOLS]:
            kind = _SYMBOL_KINDS[symbol.kind] if 0 < symbol.kind < len(_SYMBOL_KINDS) else "symbol"
            rendered.append(f"{'  ' * symbol.depth}{symbol.name} ({kind}) "
                            f"{_position(lines, symbol.range.start_line, symbol.range.start_character)}"
                            + (f" — {symbol.detail}" if symbol.detail else ""))
        if len(symbols) > MAX_SHOWN_SYMBOLS:
            rendered.append(f"… (+{len(symbols) - MAX_SHOWN_SYMBOLS} more)")
        metadata["symbols"] = len(symbols)
        return ToolResult(tool_call_id="", content="\n".join(rendered), metadata=metadata)

    def _diagnostics(self, report, path, lines, metadata) -> ToolResult:
        metadata.update(freshness=report.freshness, published_version=report.published_version)
        version = report.document_version
        if report.freshness == "none":
            return ToolResult(tool_call_id="", metadata=metadata, content=(
                f"No diagnostics published for {path} (version {version}) within "
                f"{DIAGNOSTIC_WAIT:g}s. Unknown: this is not evidence the file is clean."))
        if report.freshness == "current":
            head = f"Diagnostics for {path}, current (published for version {version})"
        elif report.freshness == "stale":
            published = report.published_version
            head = (f"STALE diagnostics for {path}: published for "
                    f"{'an earlier text' if published is None else f'version {published}'}, "
                    f"but the file is now version {version}. They do not describe the current file")
        elif report.freshness == "unversioned":
            head = (f"Diagnostics for {path} (version {version}); the server did not say which "
                    "version they describe, so currency is unknown")
        else:
            head = (f"Diagnostics for {path} with an unexpected version "
                    f"{report.published_version} (sent {version}); currency is unknown")
        if not report.diagnostics:
            return ToolResult(tool_call_id="", content=f"{head}: none.", metadata=metadata)
        rows = []
        for diag in report.diagnostics[:MAX_SHOWN_DIAGNOSTICS]:
            severity = _SEVERITIES.get(diag.severity, "unknown")
            origin = " ".join(p for p in (diag.source, diag.code) if p)
            rows.append(f"{_position(lines, diag.range.start_line, diag.range.start_character)} "
                        f"{severity}{f' [{origin}]' if origin else ''}: {diag.message}")
        if len(report.diagnostics) > MAX_SHOWN_DIAGNOSTICS:
            rows.append(f"… (+{len(report.diagnostics) - MAX_SHOWN_DIAGNOSTICS} more)")
        metadata["diagnostics"] = len(report.diagnostics)
        return ToolResult(tool_call_id="", content=f"{head}:\n" + "\n".join(rows), metadata=metadata)


def _position(lines: list[str], line: int, character: int) -> str:
    if line >= len(lines):
        return f"{line + 1}:? (UTF-16 offset {character})"
    return f"{line + 1}:{from_utf16(lines[line], character) + 1}"


def _display_path(path: str, root: str) -> str:
    for candidate, base in ((path, root), (os.path.realpath(path), os.path.realpath(root))):
        try:
            return str(Path(candidate).relative_to(base))
        except ValueError:
            continue
    return path
