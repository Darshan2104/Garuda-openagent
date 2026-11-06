"""RLM-style tool-output buffer: retain full output on disk, put a stub in context.

Large or ephemeral tool output (build logs, long `grep`, one-shot `bash`) loses its
middle bytes to head/tail truncation today. Instead, the buffer stores the full body
in the session directory and injects a compact stub (preview + pointer) into the token
window; the model retrieves what it needs with `buffer_grep` / `buffer_slice`.

Buffer files are always host-side (session dir), even for docker/remote workspaces,
so retrieval works in-process regardless of the execution environment.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import re
import shutil
import stat
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from garuda.core.sessions import default_sessions_root

if TYPE_CHECKING:
    from garuda.core.session_records import BoundedRecordReader

logger = logging.getLogger(__name__)

MAX_FILENAME_STEM = 120

PREVIEW_LINES = 20
PREVIEW_MAX_CHARS = 1500
DEFAULT_THRESHOLD_BYTES = 30_720

# Best-effort retention: buffer dirs untouched for longer than this are pruned so
# on-disk buffers don't grow without bound across sessions. Well beyond any normal
# resume window, and only the buffers/ subdir is removed (session metadata stays).
BUFFER_RETENTION_SECONDS = 14 * 24 * 3600
MAX_BUFFER_REFERENCES = 128
MAX_BUFFER_RECORD_BYTES = 8 * 1024


class ArtifactSourceDirectory(Protocol):
    """Retained authorization for an existing source directory, never a path grant."""

    def check(self) -> None: ...

    def open_directory(self, relative: Path) -> int: ...


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
                    ToolOutputBuffer(session_dir.name, root=buffers).mark_pruned()
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
    digest: str | None = None
    completeness: str = "unknown"
    omitted_source_bytes: int | None = None
    availability: str = "unknown"
    artifact: dict | None = None


def format_buffer_stub(ref: BufferRef) -> str:
    """The compact message that enters the context in place of the full output."""
    flag = " exit=error" if ref.is_error else ""
    tool = f" tool={ref.tool_name}" if ref.tool_name else ""
    header = (
        f"[buffer:{ref.buffer_id} | {ref.size_bytes} bytes | {ref.line_count} lines"
        f"{tool}{flag}]"
    )
    if ref.completeness == "complete":
        claim = "Full output stored"
    elif ref.completeness == "partial":
        claim = "Partial captured output stored"
    else:
        claim = f"Output stored with {ref.completeness} capture completeness"
    guide = (
        f'{claim}; showing the first {PREVIEW_LINES} lines. Retrieve available bytes with '
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
        self._records_root = self._root.parent / (
            "buffer-records" if self._root.name == "buffers"
            else f"{self._root.name}-buffer-records"
        )
        self._refs: dict[str, BufferRef] = {}
        self.reference_count = 0
        self.reference_keys: set[tuple[str, str]] = set()
        self.payload_read_bytes = 0
        self.reference_coverage = "complete"
        self._inspection_previews: dict[str, tuple[int, str]] = {}

    def exceeds(self, content: str, threshold: int | None = None) -> bool:
        """Whether ``content`` is large enough to belong on disk rather than in context.

        ``threshold`` overrides the session default so a caller that knows how full
        the window is can demote earlier: buffering is lossless (the body stays
        retrievable) where truncation is not, so under pressure it is strictly the
        better place for the boundary to move.
        """
        limit = self.threshold_bytes if threshold is None else threshold
        return len(content.encode("utf-8", errors="ignore")) > limit

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

    def _record_path(self, buffer_id: str) -> Path:
        return self._records_root / (self._path(buffer_id).stem + ".json")

    @contextlib.contextmanager
    def _publication_directories(self, *, create_payload: bool):
        """Bind the selected session and both storage directories without links."""
        parent = self._root.parent.absolute()
        base = parent.parent.resolve()
        base.mkdir(mode=0o700, parents=True, exist_ok=True)
        opened = []
        try:
            base_fd = os.open(base, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            opened.append(base_fd)
            with contextlib.suppress(FileExistsError):
                os.mkdir(parent.name, mode=0o700, dir_fd=base_fd)
            parent_fd = os.open(parent.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=base_fd)
            opened.append(parent_fd)
            directories = []
            for path, create in ((self._root, create_payload), (self._records_root, True)):
                if create:
                    with contextlib.suppress(FileExistsError):
                        os.mkdir(path.name, mode=0o700, dir_fd=parent_fd)
                try:
                    fd = os.open(path.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
                except FileNotFoundError:
                    if create:
                        raise
                    fd = None
                if fd is not None:
                    opened.append(fd)
                directories.append(fd)

            def check():
                bindings = [(base_fd, base, None), (parent_fd, parent.name, base_fd)]
                bindings.extend((fd, path.name, parent_fd) for fd, path in
                                zip(directories, (self._root, self._records_root), strict=True) if fd is not None)
                for fd, name, directory in bindings:
                    current = os.stat(name, dir_fd=directory, follow_symlinks=False)
                    bound = os.fstat(fd)
                    if not stat.S_ISDIR(current.st_mode) or (current.st_dev, current.st_ino) != (bound.st_dev, bound.st_ino):
                        raise OSError("artifact publication directory binding changed")

            check()
            yield directories[0], directories[1], check
        finally:
            for fd in reversed(opened):
                os.close(fd)

    @staticmethod
    def _write_all(fd: int, data: bytes) -> None:
        written = 0
        while written < len(data):
            count = os.write(fd, data[written:])
            if count <= 0:
                raise OSError("Artifact write made no progress")
            written += count

    def _atomic_write(self, path: Path, data: bytes, *, directory_fd: int | None = None) -> None:
        if directory_fd is None:
            if path.parent != self._records_root:
                raise ValueError("artifact metadata publication has an invalid directory")
            with self._publication_directories(create_payload=False) as (_, records_fd, check):
                check()
                self._atomic_write(path, data, directory_fd=records_fd)
                check()
            return
        self._write_atomic_at(path, data, directory_fd)

    def _write_atomic_at(self, path: Path, data: bytes, directory_fd: int) -> None:
        try:
            prior = os.stat(path.name, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            if not stat.S_ISREG(prior.st_mode):
                raise OSError("artifact publication target is not a regular file")
        name = ".capture-" + uuid.uuid4().hex + ".tmp"
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory_fd)
        try:
            self._write_all(fd, data)
            if os.fstat(fd).st_size != len(data):
                raise OSError("Incomplete artifact publication")
            os.fsync(fd)
            os.replace(name, path.name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
        finally:
            os.close(fd)
            with contextlib.suppress(FileNotFoundError):
                os.unlink(name, dir_fd=directory_fd)

    def _read_payload_bytes(self, buffer_id: str, *, limit: int | None = None) -> bytes:
        """Read one stable regular-file snapshot without following root or leaf links."""
        parent = directory = fd = None
        try:
            parent = os.open(
                self._root.parent,
                os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
            )
            directory = os.open(
                self._root.name,
                os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=parent,
            )
            fd = os.open(
                self._path(buffer_id).name,
                os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=directory,
            )
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode):
                raise OSError("buffer payload is not a regular file")
            data = bytearray()
            while block := os.read(fd, 65536):
                data.extend(block)
                if limit is not None and len(data) > limit:
                    raise OverflowError("buffer payload exceeds the verification budget")
            after = os.fstat(fd)
            if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                after.st_size, after.st_mtime_ns, after.st_ctime_ns
            ):
                raise OSError("buffer payload changed while being read")
            return bytes(data)
        finally:
            for opened in (fd, directory, parent):
                if opened is not None:
                    os.close(opened)

    def _read_record(self, path: Path, read_record=None, *, refuse_unsafe: bool = False) -> dict | None:
        from garuda.core.artifact_read_model import validate_descriptor
        from garuda.core.session_records import (
            BoundedSessionRecords,
            ReadBudgetExceeded,
            RecordError,
            UnsafeRecordError,
            parse_json,
        )

        try:
            if read_record is None:
                reader = BoundedSessionRecords(
                    self._root.parent.parent,
                    byte_budget=MAX_BUFFER_RECORD_BYTES + 65536,
                )
                data = reader.read(
                    Path(self._root.parent.name) / self._records_root.name / path.name,
                    limit=MAX_BUFFER_RECORD_BYTES,
                )
            else:
                data = read_record(path, MAX_BUFFER_RECORD_BYTES)
            record = validate_descriptor(parse_json(data))
            if record is None or record["owner"]["kind"] != "buffer":
                return None
            if self._record_path(record["owner"]["reference"]).name != path.name:
                return None
            return record
        except UnsafeRecordError:
            if refuse_unsafe:
                raise
            return None
        except ReadBudgetExceeded:
            if read_record is not None:
                raise
            return None
        except (OSError, ValueError, UnicodeError, RecordError):
            return None

    def store(
        self,
        buffer_id: str,
        content: str,
        tool_name: str = "",
        is_error: bool = False,
        *,
        capture: dict | None = None,
        producer_session_id: str | None = None,
    ) -> BufferRef:
        path = self._path(buffer_id)
        size_bytes = len(content.encode("utf-8"))
        lines = content.splitlines()
        preview = "\n".join(lines[:PREVIEW_LINES])[:PREVIEW_MAX_CHARS]
        from garuda.core.artifact_read_model import (
            MAX_IDENTITY_CHARS,
            descriptor,
            normalize_source_capture,
        )

        default_kind = {
            "context_archive": "context_archive",
            "invoke_subagent": "accepted_child_value",
            "task_output": "background_tail",
        }.get(tool_name, "tool_observation")
        source = normalize_source_capture(capture, default_kind=default_kind)
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        artifact = descriptor(
            owner_kind="buffer",
            owner_reference=path.stem if len(buffer_id) > MAX_IDENTITY_CHARS else buffer_id,
            storage_session_id=self.session_id,
            producer_session_id=producer_session_id or self.session_id,
            producer=tool_name or None,
            source=source,
            size_bytes=size_bytes,
            sha256=digest,
            size_provenance="recorded",
            digest_provenance="recorded",
            availability="available",
            integrity="verified",
            retention={
                "policy": "best-effort-directory-age",
                "seconds": BUFFER_RETENTION_SECONDS,
                "guaranteed_until": False,
            },
        )
        # Validate both directories before replacing either member. Publication
        # and rollback use the same held descriptors even if a path is swapped.
        with self._publication_directories(create_payload=True) as (payload_fd, records_fd, check):
            members = [(payload_fd, path), (records_fd, self._record_path(buffer_id))]
            backups = []
            publishing = False
            retained = set()
            try:
                for directory, target in members:
                    try:
                        prior = os.stat(target.name, dir_fd=directory, follow_symlinks=False)
                    except FileNotFoundError:
                        backup = None
                    else:
                        if not stat.S_ISREG(prior.st_mode):
                            raise OSError("artifact publication target is not a regular file")
                        backup = ".previous-" + uuid.uuid4().hex + ".tmp"
                        os.link(target.name, backup, src_dir_fd=directory, dst_dir_fd=directory,
                                follow_symlinks=False)
                        linked = os.stat(backup, dir_fd=directory, follow_symlinks=False)
                        if not stat.S_ISREG(linked.st_mode) or (prior.st_dev, prior.st_ino) != (linked.st_dev, linked.st_ino):
                            os.unlink(backup, dir_fd=directory)
                            raise OSError("artifact changed while preparing publication")
                    backups.append((directory, target.name, backup))
                check()
                publishing = True
                self._write_atomic_at(path, content.encode("utf-8"), payload_fd)
                check()
                self._atomic_write(self._record_path(buffer_id), json.dumps(
                    artifact, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                ).encode("utf-8"), directory_fd=records_fd)
                check()
            except BaseException as failure:
                if publishing:
                    retained = {(directory, backup) for directory, _, backup in backups if backup is not None}
                    rollback_errors = []
                    for directory, name, backup in reversed(backups):
                        try:
                            if backup is None:
                                with contextlib.suppress(FileNotFoundError):
                                    os.unlink(name, dir_fd=directory)
                            else:
                                saved = os.stat(backup, dir_fd=directory, follow_symlinks=False)
                                try:
                                    current = os.stat(name, dir_fd=directory, follow_symlinks=False)
                                except FileNotFoundError:
                                    current = None
                                if current is None or (current.st_dev, current.st_ino) != (saved.st_dev, saved.st_ino):
                                    os.replace(backup, name, src_dir_fd=directory, dst_dir_fd=directory)
                                retained.discard((directory, backup))
                        except BaseException as error:
                            rollback_errors.append(error)
                    if rollback_errors:
                        failure.add_note("Artifact rollback was not proved; original backup files were retained.")
                raise
            finally:
                for directory, _, backup in backups:
                    if backup is not None and (directory, backup) not in retained:
                        with contextlib.suppress(FileNotFoundError):
                            os.unlink(backup, dir_fd=directory)
        ref = BufferRef(
            buffer_id=buffer_id,
            path=str(path),
            size_bytes=size_bytes,
            line_count=len(lines),
            preview=preview,
            tool_name=tool_name,
            is_error=is_error,
            digest=digest,
            completeness=artifact["completeness"],
            omitted_source_bytes=artifact["omitted_source_bytes"],
            availability="available",
            artifact=artifact,
        )
        self._refs[buffer_id] = ref
        self._refs[artifact["owner"]["reference"]] = ref
        return ref

    def inherit_from(
        self, previous: "ToolOutputBuffer", *, source_binding: ArtifactSourceDirectory | None = None,
    ) -> None:
        """Copy an authorized settled source without changing its commitments."""
        from garuda.core.artifact_read_model import validate_descriptor
        from garuda.core.session_records import MAX_RECORD_FILES, BoundedSessionRecords, parse_json

        if source_binding is None:
            records = BoundedSessionRecords(previous._root.parent.parent, byte_budget=0)
            with records.selected_session(previous._root.parent.name) as binding:
                return self.inherit_from(previous, source_binding=binding)

        if self.session_id == previous.session_id or self._root.resolve() == previous._root.resolve():
            raise ValueError("resume requires distinct source and storage sessions")
        for destination in (self._root, self._records_root):
            if destination.exists() or destination.is_symlink():
                raise ValueError("resumed artifact storage already exists")
        source_binding.check()
        parent = source_binding.open_directory(Path())
        try:
            expected = os.stat(previous._root.parent, follow_symlinks=False)
            actual = os.fstat(parent)
            if (not stat.S_ISDIR(expected.st_mode)
                    or (expected.st_dev, expected.st_ino) != (actual.st_dev, actual.st_ino)):
                raise ValueError("inherited source does not match its authorized directory")
            with self._publication_directories(create_payload=True) as (payload_fd, records_fd, check):
                for source, records in ((previous._records_root, True), (previous._root, False)):
                    source_binding.check()
                    try:
                        directory = os.open(source.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                            dir_fd=parent)
                    except FileNotFoundError:
                        continue
                    try:
                        with os.scandir(directory) as entries:
                            for index, entry in enumerate(entries):
                                if index >= MAX_RECORD_FILES:
                                    raise ValueError("resumed artifacts exceed the entry budget")
                                if not entry.name.endswith(".json" if records else ".txt"):
                                    continue
                                check()
                                source_binding.check()
                                fd = os.open(entry.name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW,
                                             dir_fd=directory)
                                try:
                                    before = os.fstat(fd)
                                    if not stat.S_ISREG(before.st_mode):
                                        raise ValueError("resumed artifact is not a regular file")
                                    if records:
                                        if before.st_size > MAX_BUFFER_RECORD_BYTES:
                                            raise ValueError("resumed capture record exceeds its byte budget")
                                        raw = os.read(fd, MAX_BUFFER_RECORD_BYTES + 1)
                                        after = os.fstat(fd)
                                        if len(raw) != before.st_size or (
                                                before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                                                after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                                            raise ValueError("resumed capture record changed during its read")
                                        capture = validate_descriptor(parse_json(raw))
                                        if (capture is None or capture["owner"]["kind"] != "buffer"
                                                or capture["storage_session_id"] != previous.session_id
                                                or previous._record_path(capture["owner"]["reference"]).name != entry.name):
                                            raise ValueError("resumed capture record has an invalid binding")
                                        capture["storage_session_id"] = self.session_id
                                        capture = validate_descriptor(capture)
                                        if capture is None:
                                            raise ValueError("resumed capture record exceeds its contract")
                                        self._atomic_write(self._records_root / entry.name, json.dumps(
                                            capture, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                                        ).encode("utf-8"), directory_fd=records_fd)
                                    else:
                                        name = ".inherit-" + uuid.uuid4().hex + ".tmp"
                                        output = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                                         0o600, dir_fd=payload_fd)
                                        try:
                                            remaining = before.st_size
                                            while remaining:
                                                block = os.read(fd, min(65536, remaining))
                                                if not block:
                                                    raise ValueError("resumed artifact changed during its copy")
                                                self._write_all(output, block)
                                                remaining -= len(block)
                                            if os.read(fd, 1):
                                                raise ValueError("resumed artifact grew during its copy")
                                            os.fsync(output)
                                            after = os.fstat(fd)
                                            if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                                                    after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                                                raise ValueError("resumed artifact changed during its copy")
                                            if os.fstat(output).st_size != before.st_size:
                                                raise OSError("Incomplete inherited artifact")
                                            check()
                                            os.replace(name, entry.name, src_dir_fd=payload_fd, dst_dir_fd=payload_fd)
                                        finally:
                                            os.close(output)
                                            with contextlib.suppress(FileNotFoundError):
                                                os.unlink(name, dir_fd=payload_fd)
                                    check()
                                    source_binding.check()
                                finally:
                                    os.close(fd)
                    finally:
                        os.close(directory)
        finally:
            os.close(parent)

    def retain_inherited_commitment(self, reference: dict) -> None:
        """Restore a selected resume source's message/event commitment without new bytes."""
        from garuda.core.artifact_read_model import (
            capture_commitments_disagree,
            merge_capture_commitments,
            validate_descriptor,
        )

        capture = validate_descriptor(reference)
        if (capture is None or capture["storage_session_id"] != self.session_id
                or capture["owner"]["kind"] != "buffer"):
            raise ValueError("inherited buffer commitment has an invalid storage binding")
        path = self._record_path(capture["owner"]["reference"])
        if capture["availability"].get("reason") == "capture_commitment_conflict":
            raise ValueError("inherited capture commitments conflict with retained reference")
        if path.exists() or path.is_symlink():
            existing = self._read_record(path)
            if existing is None:
                raise ValueError("existing inherited buffer commitment is invalid")
            if capture_commitments_disagree(capture, existing):
                raise ValueError("inherited capture commitments conflict with retained reference")
            # A copied owner record may be newer than the retained transcript.
            # Preserve confirmed pruning alongside compatible historical commitments.
            capture = merge_capture_commitments(existing, capture)
            if (existing["availability"]["state"] == "unavailable"
                    and existing["availability"].get("reason") == "retention_pruned"):
                capture["availability"] = dict(existing["availability"])
            if capture == existing:
                return
        self._atomic_write(path, json.dumps(capture, ensure_ascii=False, sort_keys=True,
                                           separators=(",", ":")).encode("utf-8"))

    def require_inherited_commitments(self) -> None:
        """Incomplete source discovery cannot expose unchecked copied payloads."""
        from garuda.core.session_records import MAX_RECORD_FILES

        try:
            directory = os.open(self._root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return
        try:
            with os.scandir(directory) as entries:
                for index, entry in enumerate(entries):
                    if index >= MAX_RECORD_FILES:
                        raise ValueError("resumed artifacts exceed the entry budget")
                    if not entry.name.endswith(".txt"):
                        continue
                    # This is already a canonical payload basename. Applying
                    # raw-id sanitization again can hash a valid 133-char stem.
                    record = self._read_record(self._records_root / (entry.name[:-4] + ".json"))
                    if (not entry.is_file(follow_symlinks=False) or record is None
                            or record["storage_session_id"] != self.session_id
                            or self._path(record["owner"]["reference"]).name != entry.name
                            or record["size"]["bytes"] is None or record["digest"]["sha256"] is None):
                        raise ValueError("resume capture discovery is incomplete and copied bytes have no commitment")
        finally:
            os.close(directory)

    def _read_text(self, buffer_id: str) -> str:
        from garuda.core.session_records import UnsafeRecordError

        try:
            record = self._read_record(self._record_path(buffer_id), refuse_unsafe=True)
        except UnsafeRecordError as exc:
            raise KeyError(f"Buffer {buffer_id!r} has an unsafe capture record") from exc
        try:
            data = self._read_payload_bytes(buffer_id)
        except (FileNotFoundError, NotADirectoryError, OSError):
            raise KeyError(f"No buffer {buffer_id!r} in session {self.session_id}") from None
        if record is not None:
            if record["storage_session_id"] != self.session_id:
                raise KeyError(f"No buffer {buffer_id!r} in session {self.session_id}")
            if record["size"].get("bytes") not in {None, len(data)}:
                raise KeyError(f"Buffer {buffer_id!r} failed its recorded size check")
            digest = record["digest"].get("sha256")
            if digest is not None and hashlib.sha256(data).hexdigest() != digest:
                raise KeyError(f"Buffer {buffer_id!r} failed its recorded digest check")
        return data.decode("utf-8", errors="replace")

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

    def artifact_references(
        self, *, limit: int = MAX_BUFFER_REFERENCES, byte_budget: int | None = None,
        record_reader: BoundedRecordReader | None = None,
        retained_descriptors: list[dict] | None = None,
        verification_bytes: int | None = None,
    ) -> list[dict]:
        """Inspect owner records using one read grant, including failed reads."""
        from garuda.core.artifact_read_model import (
            MAX_ARTIFACT_SOURCE_BYTES,
            MAX_VERIFY_BYTES,
            capture_commitments_disagree,
            descriptor,
            merge_capture_commitments,
            source_capture,
            validate_descriptor,
        )
        from garuda.core.session_records import (
            BoundedSessionRecords,
            ReadBudgetExceeded,
            RecordError,
            UnsafeRecordError,
        )

        if type(limit) is not int or not 1 <= limit <= MAX_BUFFER_REFERENCES:
            raise ValueError("buffer reference limit is outside its bounded range")
        if byte_budget is not None and (type(byte_budget) is not int or byte_budget < 0):
            raise ValueError("buffer source budget is invalid")
        grant = MAX_ARTIFACT_SOURCE_BYTES if byte_budget is None else byte_budget
        reader = record_reader or BoundedSessionRecords(
            self._root.parent.parent, byte_budget=grant,
        )
        initial_remaining = reader.remaining
        grant = min(grant, initial_remaining)
        prefix = Path(self._root.parent.name)
        self.reference_coverage = "complete"
        self._inspection_previews = {}
        if verification_bytes is not None and (
            type(verification_bytes) is not int or not 0 <= verification_bytes <= MAX_VERIFY_BYTES
        ):
            raise ValueError("buffer verification budget is outside its bounded range")
        initial_verification = MAX_VERIFY_BYTES if verification_bytes is None else verification_bytes
        verify_remaining = initial_verification
        records: dict[str, dict] = {}
        conflicting: set[str] = set()

        def remember(record: dict) -> None:
            key = record["owner"]["reference"]
            previous = records.get(key)
            if previous is not None and (
                key in conflicting or capture_commitments_disagree(previous, record)
            ):
                conflicting.add(key)
                self.reference_coverage = "unknown"
                return
            if previous is not None:
                try:
                    record = merge_capture_commitments(previous, record)
                except ValueError:
                    conflicting.add(key)
                    self.reference_coverage = "unknown"
                    return
            if record["availability"].get("reason") == "capture_commitment_conflict":
                conflicting.add(key)
                self.reference_coverage = "unknown"
            records[key] = record

        for retained in retained_descriptors or []:
            retained = validate_descriptor(retained)
            if (retained is not None and retained["owner"]["kind"] == "buffer"
                    and retained["storage_session_id"] == self.session_id):
                remember(retained)
        inspected = set()
        enumeration_clipped = False
        records_unsafe = False
        unsafe_payloads: set[str] = set()

        def read_owner(path: Path, limit: int) -> bytes:
            allowance = grant - (initial_remaining - reader.remaining) - 65536
            if allowance <= 0:
                self.reference_coverage = "unknown"
                raise ReadBudgetExceeded("artifact source read budget exhausted")
            try:
                return reader.read(prefix / path.parent.name / path.name,
                                   limit=min(limit, allowance))
            except RecordError:
                self.reference_coverage = "unknown"
                raise

        def read_payload(path: Path) -> bytes:
            nonlocal verify_remaining
            # The reader reserves one 64 KiB overflow block, including failure.
            # Borrow it from the shared payload allowance before touching bytes.
            if verify_remaining <= 65536:
                self.reference_coverage = "unknown"
                raise ReadBudgetExceeded("artifact verification allowance exhausted")
            before = reader.remaining
            try:
                return read_owner(path, verify_remaining - 65536)
            finally:
                verify_remaining = max(0, verify_remaining - (before - reader.remaining))

        def names(folder: Path) -> list[str]:
            nonlocal enumeration_clipped, records_unsafe
            try:
                found, clipped = reader.names(prefix / folder.name,
                                              limit=MAX_BUFFER_REFERENCES + 1)
                enumeration_clipped = enumeration_clipped or clipped
                return found
            except UnsafeRecordError:
                if folder == self._records_root:
                    records_unsafe = True
                self.reference_coverage = "unknown"
                return []
            except RecordError as exc:
                if not isinstance(exc.__cause__, FileNotFoundError):
                    self.reference_coverage = "unknown"
                return []

        def preview(buffer_id: str, data: bytes) -> None:
            lines = data.decode("utf-8", errors="replace").splitlines()
            self._inspection_previews[buffer_id] = (
                len(lines), "\n".join(lines[:PREVIEW_LINES])[:PREVIEW_MAX_CHARS],
            )

        def exact_record(path: Path) -> tuple[dict | None, dict | None]:
            """Check this candidate's sibling even when directory discovery clipped."""
            try:
                record = self._read_record(path, read_owner, refuse_unsafe=True)
            except UnsafeRecordError:
                self.reference_coverage = "unknown"
                return None, {"state": "unavailable", "integrity": "unknown",
                              "reason": "capture_record_unsafe"}
            except ReadBudgetExceeded:
                self.reference_coverage = "unknown"
                return None, {"state": "unknown", "integrity": "unverified",
                              "reason": "capture_record_verification_budget_exhausted"}
            if record is not None and record["storage_session_id"] != self.session_id:
                self.reference_coverage = "unknown"
                return None, {"state": "unavailable", "integrity": "unknown",
                              "reason": "capture_record_storage_mismatch"}
            return record, None

        for name in names(self._records_root):
            if Path(name).suffix != ".json":
                continue
            try:
                record = self._read_record(self._records_root / name, read_owner, refuse_unsafe=True)
            except UnsafeRecordError:
                unsafe_payloads.add(Path(name).stem + ".txt")
                self.reference_coverage = "unknown"
                continue
            except ReadBudgetExceeded:
                self.reference_coverage = "unknown"
                break
            if record is not None and record["storage_session_id"] == self.session_id:
                remember(record)
            else:
                self.reference_coverage = "unknown"

        recorded_payloads = {self._path(record["owner"]["reference"]).name
                             for record in records.values()}
        for name in names(self._root):
            if Path(name).suffix != ".txt":
                continue
            buffer_id = Path(name).stem
            if name in recorded_payloads:
                continue
            source = source_capture(kind="legacy_buffer", completeness="unknown")
            if records_unsafe or name in unsafe_payloads:
                checked, refusal = None, {"state": "unavailable", "integrity": "unknown",
                                           "reason": "capture_record_unsafe"}
            else:
                # This name is already a canonical payload basename, so select
                # the exact sibling without hashing the stem a second time.
                checked, refusal = exact_record(self._records_root / (Path(name).stem + ".json"))
            if checked is not None:
                remember(checked)
                continue
            inspected.add(buffer_id)
            if refusal is not None:
                records[buffer_id] = descriptor(
                    owner_kind="buffer", owner_reference=buffer_id,
                    storage_session_id=self.session_id, producer_session_id=None,
                    producer=None, source=source, size_bytes=None, sha256=None,
                    size_provenance="unknown", digest_provenance="unknown",
                    availability=refusal["state"], integrity=refusal["integrity"],
                    reason=refusal["reason"], retention={"policy": "legacy-unknown"},
                )
                continue
            try:
                data = read_payload(self._root / name)
            except RecordError as exc:
                budget_exhausted = isinstance(exc, ReadBudgetExceeded)
                records[buffer_id] = descriptor(
                    owner_kind="buffer", owner_reference=buffer_id,
                    storage_session_id=self.session_id, producer_session_id=None,
                    producer=None, source=source, size_bytes=None, sha256=None,
                    size_provenance="unknown", digest_provenance="unknown",
                    availability="unknown" if budget_exhausted else "unavailable",
                    integrity="unverified" if budget_exhausted else "unknown",
                    reason="verification_budget_exhausted" if budget_exhausted
                        else "payload_unreadable_or_symlinked",
                    retention={"policy": "legacy-unknown"},
                )
                continue
            preview(buffer_id, data)
            records[buffer_id] = descriptor(
                owner_kind="buffer", owner_reference=buffer_id,
                storage_session_id=self.session_id, producer_session_id=None,
                producer=None, source=source, size_bytes=len(data),
                sha256=hashlib.sha256(data).hexdigest(),
                size_provenance="observed", digest_provenance="observed",
                availability="available", integrity="verified",
                retention={"policy": "legacy-unknown", "seconds": BUFFER_RETENTION_SECONDS,
                           "guaranteed_until": False},
            )

        projected = []
        for record in sorted(records.values(), key=lambda r: r["owner"]["reference"]):
            current = json.loads(json.dumps(record))
            buffer_id = current["owner"]["reference"]
            if buffer_id in conflicting:
                current["availability"] = {"state": "unavailable", "integrity": "failed",
                                           "reason": "capture_commitment_conflict"}
                projected.append(current)
                continue
            if buffer_id in inspected:
                projected.append(current)
                continue
            if records_unsafe or self._path(buffer_id).name in unsafe_payloads:
                current["availability"] = {"state": "unavailable", "integrity": "unknown",
                                           "reason": "capture_record_unsafe"}
                projected.append(current)
                continue
            pruned = (current["availability"]["state"] == "unavailable"
                      and current["availability"].get("reason") == "retention_pruned")
            checked, refusal = exact_record(self._record_path(buffer_id))
            if refusal is not None:
                if not (pruned and refusal["state"] == "unknown"):
                    current["availability"] = refusal
                projected.append(current)
                continue
            if checked is not None:
                try:
                    # Preserve the adaptation's availability and rendering facts.
                    current = merge_capture_commitments(checked, current)
                except ValueError:
                    self.reference_coverage = "unknown"
                    current["availability"] = {"state": "unavailable", "integrity": "failed",
                                               "reason": "capture_commitment_conflict"}
                    projected.append(current)
                    continue
                if (checked["availability"]["state"] == "unavailable"
                        and checked["availability"].get("reason") == "retention_pruned"):
                    current["availability"] = dict(checked["availability"])
            pruned = (current["availability"]["state"] == "unavailable"
                      and current["availability"].get("reason") == "retention_pruned")
            expected_size = current["size"].get("bytes")
            if verify_remaining <= 0 or (
                expected_size is not None and expected_size > verify_remaining
            ):
                self.reference_coverage = "unknown"
                if not pruned:
                    current["availability"] = {"state": "unknown", "integrity": "unverified",
                                               "reason": "verification_budget_exhausted"}
            else:
                try:
                    data = read_payload(self._path(buffer_id))
                except RecordError as exc:
                    cause = exc.__cause__
                    missing = isinstance(cause, (FileNotFoundError, NotADirectoryError))
                    unavailable = not isinstance(exc, ReadBudgetExceeded)
                    current["availability"] = {
                        "state": "unavailable" if unavailable or pruned else "unknown",
                        "integrity": "unknown" if unavailable or pruned else "unverified",
                        "reason": "retention_pruned" if pruned else "payload_missing" if missing
                            else "payload_unreadable_or_symlinked" if unavailable
                            else "verification_budget_exhausted",
                    }
                else:
                    expected_digest = current["digest"].get("sha256")
                    if expected_size is not None and len(data) != expected_size:
                        current["availability"] = {"state": "unavailable", "integrity": "failed",
                                                   "reason": "size_mismatch"}
                    elif expected_digest is not None and hashlib.sha256(data).hexdigest() != expected_digest:
                        current["availability"] = {"state": "unavailable", "integrity": "failed",
                                                   "reason": "digest_mismatch"}
                    else:
                        preview(buffer_id, data)
                        current["availability"] = {"state": "available", "integrity": "verified"}
            projected.append(current)
        self.payload_read_bytes = initial_verification - verify_remaining
        self.reference_count = len(projected)
        self.reference_keys = {(item["owner"]["kind"], item["owner"]["reference"])
                               for item in projected}
        if enumeration_clipped:
            self.reference_coverage = "clipped"
        return projected[:limit]

    def list_buffers(self) -> list[BufferRef]:
        """Known captures, including legacy and currently unavailable references."""
        refs = []
        for artifact in self.artifact_references():
            bid = artifact["owner"]["reference"]
            cached = self._refs.get(bid)
            preview = cached.preview if cached else ""
            line_count = cached.line_count if cached else 0
            if bid in self._inspection_previews:
                line_count, preview = self._inspection_previews[bid]
            refs.append(BufferRef(
                buffer_id=cached.buffer_id if cached else bid,
                path=str(self._path(bid)),
                size_bytes=artifact["size"].get("bytes") or 0,
                line_count=line_count,
                preview=preview,
                tool_name=artifact.get("producer") or "",
                is_error=cached.is_error if cached else False,
                digest=artifact["digest"].get("sha256"),
                completeness=artifact["completeness"],
                omitted_source_bytes=artifact["omitted_source_bytes"],
                availability=artifact["availability"]["state"],
                artifact=artifact,
            ))
        return refs

    def mark_pruned(self) -> None:
        """Record owner-confirmed retention before the payload directory is removed."""
        try:
            paths = []
            for index, path in enumerate(self._records_root.iterdir()):
                if index >= MAX_BUFFER_REFERENCES:
                    break
                if path.suffix == ".json":
                    paths.append(path)
        except OSError:
            return
        for path in paths:
            record = self._read_record(path)
            if record is None:
                continue
            record["availability"] = {
                "state": "unavailable", "integrity": "unknown", "reason": "retention_pruned",
            }
            try:
                self._atomic_write(path, json.dumps(
                    record, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                ).encode("utf-8"))
            except OSError:
                logger.debug("Could not record pruned buffer %s", path, exc_info=True)
