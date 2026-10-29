"""Optional bounded UTF-8 reads; ordinary Environment.read_file stays compatible."""

import codecs
import os
import stat
import threading
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

CHUNK_BYTES = 8192
MAX_SCAN_BYTES = 64 * 1024 * 1024
MAX_PAGE_LINES = 2000
MAX_LINE_CHARS = 2000
_BREAKS = frozenset('\n\r\v\f\x1c\x1d\x1e\x85\u2028\u2029')


@dataclass(frozen=True)
class TextPage:
    lines: tuple[str, ...]
    offset: int
    total_lines: int | None
    has_more: bool
    bytes_read: int


@runtime_checkable
class BoundedTextEnvironment(Protocol):
    async def read_text_page(self, path: str, *, offset: int = 1,
                             limit: int = 2000, tail: bool = False) -> TextPage:
        """Read UTF-8 strictly, with bounded memory/I/O, or raise NotImplementedError.

        Line boundaries match str.splitlines; long lines retain at most 2001
        characters so consumers can mark truncation. Unknown totals are None.
        Backends must enforce their ordinary file path policy and settle on cancel.
        """
        ...


def read_local_page(path: Path, offset: int, limit: int, tail: bool,
                    cancelled: threading.Event) -> TextPage:
    if not 1 <= limit <= MAX_PAGE_LINES or offset < 1 or (tail and offset != 1):
        raise ValueError('Require offset >= 1, limit 1..2000, and no offset with tail')
    selected: deque[str] = deque(maxlen=limit)
    line: list[str] = []
    count = scanned = 0
    skip_lf = False
    decoder = codecs.getincrementaldecoder('utf-8')('strict')

    def emit():
        nonlocal count
        count += 1
        if tail or offset <= count < offset + limit:
            selected.append(''.join(line))
        line.clear()

    # Nonblocking open avoids getting stuck opening a FIFO; only regular files
    # support this capability. The caller resolves through its normal path policy.
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb', buffering=0) as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise OSError('Bounded text reads require a regular file')
        while True:
            if cancelled.is_set():
                raise InterruptedError('Text read cancelled')
            if scanned >= MAX_SCAN_BYTES:
                # Do not consume another byte just to test EOF at the scan ceiling.
                if os.fstat(stream.fileno()).st_size <= scanned:
                    data = b''
                else:
                    raise OSError('Text read exceeds 64 MiB scan budget; use a smaller offset')
            else:
                data = stream.read(min(CHUNK_BYTES, MAX_SCAN_BYTES - scanned))
            scanned += len(data)
            eof = not data
            text = decoder.decode(data, final=eof)
            for char in text:
                if skip_lf:
                    skip_lf = False
                    if char == '\n':
                        continue
                if char in _BREAKS:
                    emit()
                    skip_lf = char == '\r'
                elif len(line) <= MAX_LINE_CHARS:
                    line.append(char)
            # A short regular-file read isn't proof of EOF under concurrent edits.
            # Small files are exhausted to preserve exact existing count semantics.
            if not tail and count >= offset + limit and scanned >= CHUNK_BYTES:
                return TextPage(tuple(selected), offset, None, True, scanned)
            if eof:
                if line:
                    emit()
                start = max(1, count - len(selected) + 1) if tail else offset
                return TextPage(tuple(selected), start, count,
                                count >= start + len(selected), scanned)
