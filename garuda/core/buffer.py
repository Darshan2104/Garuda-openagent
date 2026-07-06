"""RLM-style tool-output buffer: retain full output on disk, put a stub in context.

Large or ephemeral tool output (build logs, long `grep`, one-shot `bash`) loses its
middle bytes to head/tail truncation today. Instead, the buffer stores the full body
in the session directory and injects a compact stub (preview + pointer) into the token
window; the model retrieves what it needs with `buffer_grep` / `buffer_slice`.

Buffer files are always host-side (session dir), even for docker/remote workspaces,
so retrieval works in-process regardless of the execution environment.
"""

import hashlib
import logging
import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from garuda.core.sessions import default_sessions_root

logger = logging.getLogger(__name__)

MAX_FILENAME_STEM = 120

PREVIEW_LINES = 20
PREVIEW_MAX_CHARS = 1500
DEFAULT_THRESHOLD_BYTES = 30_720

# Best-effort retention: buffer dirs untouched for longer than this are pruned so
# on-disk buffers don't grow without bound across sessions. Well beyond any normal
# resume window, and only the buffers/ subdir is removed (session metadata stays).
BUFFER_RETENTION_SECONDS = 14 * 24 * 3600


def _prune_old_buffer_dirs(sessions_root: Path) -> None:
    """Remove stale ``<session>/buffers`` dirs (best-effort; never raises)."""
    try:
        if not sessions_root.exists():
            return
        cutoff = time.time() - BUFFER_RETENTION_SECONDS
        for session_dir in sessions_root.iterdir():
            buffers = session_dir / "buffers"
            try:
                if buffers.is_dir() and buffers.stat().st_mtime < cutoff:
                    shutil.rmtree(buffers, ignore_errors=True)
            except OSError:
                continue
    except Exception:
        logger.debug("Buffer retention prune failed", exc_info=True)


@dataclass
class BufferRef:
    buffer_id: str
    path: str
    size_bytes: int
    line_count: int
    preview: str
    tool_name: str = ""
    is_error: bool = False


def format_buffer_stub(ref: BufferRef) -> str:
    """The compact message that enters the context in place of the full output."""
    flag = " exit=error" if ref.is_error else ""
    tool = f" tool={ref.tool_name}" if ref.tool_name else ""
    header = (
        f"[buffer:{ref.buffer_id} | {ref.size_bytes} bytes | {ref.line_count} lines"
        f"{tool}{flag}]"
    )
    guide = (
        f'Full output stored; showing the first {PREVIEW_LINES} lines. Retrieve the rest with '
        f'buffer_grep(buffer_id="{ref.buffer_id}", pattern="..."), '
        f'buffer_slice(buffer_id="{ref.buffer_id}", start_line=N, end_line=M), '
        f"or buffer_list to see all buffers."
    )
    return f"{header}\n{guide}\n--- preview ---\n{ref.preview}"


class ToolOutputBuffer:
    def __init__(
        self,
        session_id: str,
        threshold_bytes: int = DEFAULT_THRESHOLD_BYTES,
        root: str | Path | None = None,
    ):
        self.session_id = session_id
        self.threshold_bytes = threshold_bytes
        if root:
            self._root = Path(root)
        else:
            sessions_root = default_sessions_root()
            self._root = sessions_root / session_id / "buffers"
            _prune_old_buffer_dirs(sessions_root)  # bound cross-session disk growth
        self._refs: dict[str, BufferRef] = {}

    def exceeds(self, content: str) -> bool:
        return len(content.encode("utf-8", errors="ignore")) > self.threshold_bytes

    def _path(self, buffer_id: str) -> Path:
        # Keep the stem filesystem-safe and bounded — some providers (Gemini)
        # return very long tool-call ids that would exceed the OS name limit.
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", buffer_id) or "buffer"
        # If sanitization changed the id (e.g. "a/b" and "a_b" both -> "a_b") or it
        # overflows the name limit, disambiguate with a hash of the RAW id so two
        # distinct buffers can never collide onto one file (silent overwrite).
        if safe != buffer_id or len(safe) > MAX_FILENAME_STEM:
            digest = hashlib.sha1(buffer_id.encode("utf-8")).hexdigest()[:12]
            safe = f"{safe[:MAX_FILENAME_STEM]}_{digest}" if len(safe) <= MAX_FILENAME_STEM else digest
        return self._root / f"{safe}.txt"

    def store(self, buffer_id: str, content: str, tool_name: str = "", is_error: bool = False) -> BufferRef:
        self._root.mkdir(parents=True, exist_ok=True)
        path = self._path(buffer_id)
        path.write_text(content, encoding="utf-8")
        lines = content.splitlines()
        preview = "\n".join(lines[:PREVIEW_LINES])[:PREVIEW_MAX_CHARS]
        ref = BufferRef(
            buffer_id=buffer_id,
            path=str(path),
            size_bytes=len(content.encode("utf-8", errors="ignore")),
            line_count=len(lines),
            preview=preview,
            tool_name=tool_name,
            is_error=is_error,
        )
        self._refs[buffer_id] = ref
        return ref

    def _read_text(self, buffer_id: str) -> str:
        path = self._path(buffer_id)
        if not path.exists():
            raise KeyError(f"No buffer {buffer_id!r} in session {self.session_id}")
        return path.read_text(encoding="utf-8", errors="replace")

    def read(self, buffer_id: str) -> str:
        return self._read_text(buffer_id)

    def grep(self, buffer_id: str, pattern: str, max_results: int = 100) -> list[str]:
        regex = re.compile(pattern)
        matches: list[str] = []
        for lineno, line in enumerate(self._read_text(buffer_id).splitlines(), start=1):
            if regex.search(line):
                matches.append(f"{lineno}:{line}")
                if len(matches) >= max_results:
                    break
        return matches

    def slice(self, buffer_id: str, start_line: int, end_line: int) -> str:
        lines = self._read_text(buffer_id).splitlines()
        start = max(1, start_line)
        end = min(len(lines), end_line)
        if start > end:
            return ""
        return "\n".join(f"{i}:{lines[i - 1]}" for i in range(start, end + 1))

    def list_buffers(self) -> list[BufferRef]:
        """All buffers for this session, including any restored from disk on resume."""
        refs = dict(self._refs)
        if self._root.exists():
            for file in sorted(self._root.glob("*.txt")):
                bid = file.stem
                if bid in refs:
                    continue
                text = file.read_text(encoding="utf-8", errors="replace")
                lines = text.splitlines()
                refs[bid] = BufferRef(
                    buffer_id=bid,
                    path=str(file),
                    size_bytes=file.stat().st_size,
                    line_count=len(lines),
                    preview="\n".join(lines[:PREVIEW_LINES])[:PREVIEW_MAX_CHARS],
                )
        return list(refs.values())
