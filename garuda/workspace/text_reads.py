"""Optional bounded UTF-8 reads; ordinary Environment.read_file stays compatible."""

import codecs
import hashlib
import io
import os
import re
import stat
import threading
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

CHUNK_BYTES = 8192
# Forward reads decode this many bytes at a time and split them in C.
SCAN_BLOCK_BYTES = 1024 * 1024
# A tail whose region starts this close to byte 0 is rescanned from the start so
# small files keep exact line numbers and totals.
EXACT_TAIL_BYTES = 1024 * 1024
MAX_PAGE_LINES = 2000
MAX_LINE_CHARS = 2000
_BREAKS = frozenset('\n\r\v\f\x1c\x1d\x1e\x85\u2028\u2029')
# UTF-8 encodings of _BREAKS; a CR is a break of its own only when no LF follows.
_BREAK_BYTES = re.compile(rb'[\n\x0b\x0c\x1c-\x1e]|\r(?!\n)|\xc2\x85|\xe2\x80[\xa8\xa9]')


@dataclass(frozen=True)
class TextPage:
    lines: tuple[str, ...]
    offset: int | None
    total_lines: int | None
    has_more: bool
    bytes_read: int
    # SHA-256 (hex) of the UTF-8 encoding of the whole file's text exactly as the
    # backend's read_file returns it (strict UTF-8, universal newlines), taken in
    # the same pass as the page. None when this read did not cover the whole file.
    content_sha256: str | None = None


@runtime_checkable
class BoundedTextEnvironment(Protocol):
    async def read_text_page(self, path: str, *, offset: int = 1,
                             limit: int = 2000, tail: bool = False,
                             digest: bool = False) -> TextPage:
        """Read UTF-8 strictly, with bounded memory/I/O, or raise NotImplementedError.

        digest=True asks for TextPage.content_sha256 of the whole file: a forward
        page then keeps streaming (memory stays bounded) to EOF instead of
        stopping after the page. Without it the digest is reported only when the
        page already covered the whole file. A backend that cannot hash the file
        in the same pass returns None (unknown), never a page-only digest.

        Line boundaries match str.splitlines; long lines retain at most 2001
        characters so consumers can mark truncation. Unknown totals are None;
        a tail of a large file also has an unknown (None) first-line offset.
        Backends must enforce their ordinary file path policy and settle on cancel.
        """
        ...


def _tail_start(stream, limit: int, cancelled: threading.Event) -> tuple[int, int]:
    """Seek back from EOF to a line start followed by at least `limit` lines."""
    pos = os.fstat(stream.fileno()).st_size
    after = b''
    found = scanned = 0
    while pos > 0:
        if cancelled.is_set():
            raise InterruptedError('Text read cancelled')
        size = min(CHUNK_BYTES, pos)
        pos -= size
        stream.seek(pos)
        block = stream.read(size)
        scanned += len(block)
        # `after` lets a CR or multi-byte break at the block's end see its successor.
        ends = [match.end() for match in _BREAK_BYTES.finditer(block + after)
                if match.start() < len(block)]
        for end in reversed(ends):
            found += 1
            if found > limit:
                return pos + end, scanned
        after = block[:2]
    return 0, scanned


def read_local_page(path: Path, offset: int, limit: int, tail: bool,
                    cancelled: threading.Event, digest: bool = False) -> TextPage:
    if not 1 <= limit <= MAX_PAGE_LINES or offset < 1 or (tail and offset != 1):
        raise ValueError('Require offset >= 1, limit 1..2000, and no offset with tail')
    selected: deque[str] = deque(maxlen=limit)
    carry = ''  # head (at most 2001 chars) of the line still open at a block end
    count = scanned = 0
    skip_lf = False
    decoder = codecs.getincrementaldecoder('utf-8')('strict')
    # The digest covers what LocalEnvironment.read_file returns (Path.read_text:
    # strict UTF-8 with universal newlines), so it equals the digest an edit
    # computes from its own full read of unchanged content.
    hasher = hashlib.sha256()
    newlines = io.IncrementalNewlineDecoder(None, translate=True)

    # Nonblocking open avoids getting stuck opening a FIFO; only regular files
    # support this capability. The caller resolves through its normal path policy.
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb', buffering=0) as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise OSError('Bounded text reads require a regular file')
        start_byte = 0
        if tail:
            # Tail cost follows the tail's size, not the file's: only the bytes
            # after the located line start are decoded, so earlier lines stay
            # unvalidated and uncounted.
            start_byte, scanned = _tail_start(stream, limit, cancelled)
            if start_byte <= EXACT_TAIL_BYTES:
                start_byte = 0
            stream.seek(start_byte)
        # Forward pages stream without a total-byte ceiling: memory stays bounded
        # by one block plus the retained page, and I/O never exceeds the
        # whole-file read it replaces. Lines before the page are only counted.
        forward = 0
        while True:
            if cancelled.is_set():
                raise InterruptedError('Text read cancelled')
            data = stream.read(SCAN_BLOCK_BYTES)
            forward += len(data)
            eof = not data
            text = decoder.decode(data, final=eof)
            if not start_byte:
                hasher.update(newlines.decode(text, final=eof).encode('utf-8'))
            if skip_lf and text:
                # The CR ending the previous block already closed its line.
                skip_lf = False
                if text[0] == '\n':
                    text = text[1:]
            if text:
                parts = text.splitlines(keepends=True)
                open_part = None if parts[-1][-1] in _BREAKS else parts.pop()
                skip_lf = open_part is None and text[-1] == '\r'
                first = count + 1
                low, high = ((max(0, len(parts) - limit), len(parts)) if tail else
                             (max(0, offset - first), min(len(parts), offset + limit - first)))
                for index in range(low, high):
                    part = parts[index]
                    body = part[:-2] if part.endswith('\r\n') else part[:-1]
                    if index == 0:
                        body = carry + body[:MAX_LINE_CHARS + 1]
                    selected.append(body[:MAX_LINE_CHARS + 1])
                if parts:
                    count += len(parts)
                    carry = ''
                if open_part is not None:
                    carry = (carry + open_part[:MAX_LINE_CHARS + 1])[:MAX_LINE_CHARS + 1]
            # A short regular-file read isn't proof of EOF under concurrent edits.
            # Small files are exhausted to preserve exact existing count semantics.
            if not tail and not digest and count >= offset + limit and forward >= CHUNK_BYTES:
                return TextPage(tuple(selected), offset, None, True, forward)
            if eof:
                if carry:
                    count += 1
                    if tail or offset <= count < offset + limit:
                        selected.append(carry)
                if start_byte:
                    return TextPage(tuple(selected), None, None, False, scanned + forward)
                start = max(1, count - len(selected) + 1) if tail else offset
                return TextPage(tuple(selected), start, count,
                                count >= start + len(selected), scanned + forward,
                                hasher.hexdigest())
