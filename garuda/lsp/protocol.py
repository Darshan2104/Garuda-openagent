"""The optional language-service boundary: semantic navigation for agent tools.

A :class:`LanguageService` answers definition, references, hover, document
symbol and diagnostic questions about workspace files, and returns rename or
code-action WorkspaceEdits bound to its request snapshots. The agent-facing tool
(:mod:`garuda.tools.language`) depends only on this protocol; the stdio JSON-RPC
client in :mod:`garuda.lsp.client` is one implementation.

Positions crossing this boundary are LSP-native (0-based line, UTF-16
character) and documents are named by absolute path; the tool layer owns the
conversion to what the model sees. Every request is labelled read-only, but a
read-labelled LSP request does not prove the server writes no caches or index
files, or that it stays inside the workspace: the server is a trusted local
process, not a confined one.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol, runtime_checkable


class ServiceState(str, Enum):
    CONFIGURED = "configured"  # not launched; capabilities unknown
    STARTING = "starting"
    READY = "ready"
    FAILED = "failed"  # startup failed or the server died; never restarted
    CLOSED = "closed"


class LanguageServiceError(Exception):
    """A request could not be answered; the message says why, for the model."""


class UnsupportedRequest(LanguageServiceError):
    """The server's declared capabilities do not include this request."""


# Request kind -> the server capability that must be present to send it.
REQUEST_CAPABILITIES = {
    "definition": "definitionProvider",
    "references": "referencesProvider",
    "hover": "hoverProvider",
    "symbols": "documentSymbolProvider",
    "rename": "renameProvider",
    "code_action": "codeActionProvider",
}


@dataclass(frozen=True)
class Range:
    start_line: int
    start_character: int  # UTF-16 code units
    end_line: int
    end_character: int


@dataclass(frozen=True)
class DocumentSnapshot:
    path: str
    version: int
    text: str


@dataclass(frozen=True)
class WorkspaceEditResponse:
    edit: dict | None
    documents: tuple[DocumentSnapshot, ...]


@dataclass(frozen=True)
class Location:
    uri: str
    range: Range


@dataclass(frozen=True)
class Symbol:
    name: str
    kind: int
    range: Range
    depth: int = 0
    detail: str = ""


@dataclass(frozen=True)
class Diagnostic:
    range: Range
    message: str
    severity: int | None = None
    source: str = ""
    code: str = ""


@dataclass(frozen=True)
class DiagnosticsReport:
    """Diagnostics bound to the document version Garuda last sent.

    ``freshness`` is ``current`` only when the server stated it published for
    exactly ``document_version``. ``stale`` means it published for an older
    version; ``unversioned`` means it did not say which version (so currency
    cannot be shown); ``none`` means nothing was published within the wait.
    Only ``current`` diagnostics describe the file as it is now.
    """

    document_version: int
    published_version: int | None
    freshness: str
    diagnostics: tuple[Diagnostic, ...] = ()


@dataclass(frozen=True)
class ServiceStatus:
    name: str
    state: ServiceState
    extensions: tuple[str, ...]
    command: str
    detail: str = ""
    # Request kinds the server declared; empty while not started (unknown).
    supported: tuple[str, ...] = ()
    unsupported: tuple[str, ...] = ()
    root: str | None = None


@runtime_checkable
class LanguageService(Protocol):
    """One language server for a set of file extensions, owned by one run."""

    name: str
    extensions: tuple[str, ...]

    def status(self) -> ServiceStatus:
        """Current state. Never launches anything."""
        ...

    async def sync(self, path: str, text: str, root: str) -> int:
        """Open or update ``path`` with ``text``; return the version now sent.

        Starts the server on first use, bound to workspace ``root``.
        """
        ...

    async def definition(self, path: str, line: int, character: int) -> list[Location]: ...

    async def references(
        self, path: str, line: int, character: int, include_declaration: bool
    ) -> list[Location]: ...

    async def hover(self, path: str, line: int, character: int) -> str | None: ...

    async def symbols(self, path: str) -> list[Symbol]: ...

    async def diagnostics(self, path: str, wait: float) -> DiagnosticsReport: ...

    async def rename(
        self, path: str, line: int, character: int, new_name: str
    ) -> WorkspaceEditResponse: ...

    async def code_actions(self, path: str, selection: Range) -> list[dict]: ...

    async def code_action(
        self, path: str, selection: Range, title: str
    ) -> WorkspaceEditResponse: ...

    async def close(self) -> None: ...
