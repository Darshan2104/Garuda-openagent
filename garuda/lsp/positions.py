"""File URIs and position conversion between Garuda and the language server protocol.

Garuda's tools speak 1-based lines and 1-based columns counted in Unicode
characters (code points), which is what ``read_file`` shows. LSP positions are
0-based and count UTF-16 code units, and LSP lines end at ``\\n``, ``\\r\\n`` or
``\\r`` only — not at the other separators ``str.splitlines`` honours (form
feed, ``\\u2028``...). Converting with the wrong line model or unit misplaces
every position after an astral character or an unusual separator, so all
conversion goes through this module.
"""

from __future__ import annotations

import re
import urllib.parse
from pathlib import Path, PurePosixPath

_LINE_BREAK = re.compile(r"\r\n|\r|\n")


def lsp_lines(text: str) -> list[str]:
    """Split ``text`` into lines the way LSP counts them (separators removed)."""
    return _LINE_BREAK.split(text)


def utf16_length(text: str) -> int:
    return sum(2 if ord(ch) > 0xFFFF else 1 for ch in text)


def to_utf16(line_text: str, column: int) -> int:
    """UTF-16 offset of 0-based code-point ``column`` within ``line_text``."""
    return utf16_length(line_text[: max(0, column)])


def from_utf16(line_text: str, offset: int) -> int:
    """0-based code-point column for a UTF-16 ``offset``.

    An offset past the end of the line means the end of the line (the LSP rule).
    An offset that splits a surrogate pair rounds down to the start of that
    character rather than inventing a half-character position.
    """
    units = 0
    for index, ch in enumerate(line_text):
        width = 2 if ord(ch) > 0xFFFF else 1
        if units + width > offset:
            return index
        units += width
    return len(line_text)


def path_to_uri(path: str | Path) -> str:
    """``file://`` URI for an absolute path, percent-escaping as RFC 3986 requires."""
    return Path(path).absolute().as_uri()


def uri_to_path(uri: str) -> str | None:
    """Absolute local path for a ``file`` URI, or ``None`` for any other URI.

    ``None`` is deliberate: a ``jdt://`` or ``untitled:`` location names no file
    Garuda can read, so callers report the URI as given instead of guessing.
    """
    try:
        parts = urllib.parse.urlsplit(uri)
    except ValueError:
        return None
    if parts.scheme.lower() != "file" or parts.netloc not in ("", "localhost"):
        return None
    path = urllib.parse.unquote(parts.path)
    if not path.startswith("/") or "\x00" in path:
        return None
    return str(PurePosixPath(path))
