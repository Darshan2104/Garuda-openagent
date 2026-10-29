"""Bounded read-only session records, anchored by no-follow descriptors."""

from __future__ import annotations

import hashlib
import json
import math
import os
import stat
from pathlib import Path

from garuda.core.sessions import SessionStore, validate_session_ref

MAX_RECORD_BYTES = 4 * 1024 * 1024
MAX_RECORD_FILES = 10_000


class RecordError(ValueError):
    pass


def session_reference_digest(store, session_id: str, *, root_fd=None, session_fd=None) -> str:
    """Opaque binding to the actual store/session directories, including aliases."""
    session_id = validate_session_ref(session_id)
    opened = []
    try:
        if root_fd is None:
            root_fd = _directory(store, ())
            opened.append(root_fd)
        if session_fd is None:
            session_fd = os.open(session_id, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
                                 dir_fd=root_fd)
            opened.append(session_fd)
        root_info, session_info = os.fstat(root_fd), os.fstat(session_fd)
        binding = {"reference": session_id, "store": [root_info.st_dev, root_info.st_ino],
                   "session": [session_info.st_dev, session_info.st_ino]}
        return hashlib.sha256(json.dumps(binding, sort_keys=True).encode()).hexdigest()
    except OSError as exc:
        raise RecordError("selected session reference identity is unavailable") from exc
    finally:
        for fd in reversed(opened):
            os.close(fd)


def _directory(store, parts):
    directory = os.open(store.root.resolve(), os.O_RDONLY | os.O_DIRECTORY | getattr(os, 'O_NOFOLLOW', 0))
    try:
        for part in parts:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | getattr(os, 'O_NOFOLLOW', 0), dir_fd=directory)
            os.close(directory)
            directory = child
        return directory
    except BaseException:
        os.close(directory)
        raise


def _parts(relative):
    parts = Path(relative).parts
    if not parts or Path(relative).is_absolute() or any(p in {'.', '..'} for p in parts):
        raise RecordError('record path is outside the selected session store')
    return parts


def read_bytes(store, relative, *, limit=MAX_RECORD_BYTES):
    parts = _parts(relative)
    directory = None
    try:
        directory = _directory(store, parts[:-1])
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NONBLOCK | getattr(os, 'O_NOFOLLOW', 0), dir_fd=directory)
        try:
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode):
                raise RecordError('record is not a regular file')
            data = bytearray()
            while block := os.read(fd, 65536):
                data.extend(block)
                if len(data) > limit:
                    raise RecordError('record exceeds its read budget')
            after = os.fstat(fd)
            if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                raise RecordError('record changed while being read')
            return bytes(data)
        finally:
            os.close(fd)
    except OSError as exc:
        raise RecordError('record is missing, unreadable or symlinked') from exc
    finally:
        if directory is not None:
            os.close(directory)


def parse_json(data):
    def unique(pairs):
        out = {}
        for key, value in pairs:
            if key in out:
                raise ValueError('duplicate key')
            out[key] = value
        return out

    def invalid_constant(value):
        raise ValueError(f"invalid JSON constant {value}")

    def finite_float(value):
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("nonfinite JSON number")
        return number

    try:
        result = json.loads(data, object_pairs_hook=unique, parse_constant=invalid_constant,
                            parse_float=finite_float)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise RecordError('record is not valid unique-key JSON') from exc
    if not isinstance(result, dict):
        raise RecordError('record must be a mapping')
    return result


def read_json(store, relative):
    return parse_json(read_bytes(store, relative))


def list_json(store, relative, *, entry_limit=MAX_RECORD_FILES):
    directory = None
    try:
        directory = _directory(store, _parts(relative))
        names = []
        with os.scandir(directory) as entries:
            for index, entry in enumerate(entries):
                if index >= entry_limit:
                    raise RecordError("selected records exceed the directory entry budget")
                if entry.name.endswith('.json'):
                    names.append(entry.name)
                    if len(names) > MAX_RECORD_FILES:
                        raise RecordError('selected records exceed the file-count budget')
        return sorted(names)
    except OSError as exc:
        raise RecordError('selected record directory is missing, unreadable or symlinked') from exc
    finally:
        if directory is not None:
            os.close(directory)


class ReadOnlySessions(SessionStore):
    """One projection's bounded metadata snapshot; never allocates or repairs."""

    def __init__(self, root):
        super().__init__(root)
        self._records = {}

    def load_meta(self, session_id):
        session_id = validate_session_ref(session_id)
        if session_id not in self._records:
            self._records[session_id] = read_json(self, Path(session_id) / 'meta.json')
        return self._records[session_id]


class BoundedSessionRecords(ReadOnlySessions):
    """A read-only metadata snapshot with one shared allowance for every source."""

    def __init__(self, root, *, byte_budget: int):
        super().__init__(root)
        self.remaining = byte_budget

    def read(self, relative: Path, limit: int = MAX_RECORD_BYTES) -> bytes:
        # Oversize detection can consume one extra 64 KiB chunk. Reserve it
        # before reading, including failures, to keep the aggregate bound real.
        allowance = min(limit, self.remaining - 65536)
        if allowance <= 0:
            raise RecordError("aggregate source read budget exhausted")
        reserved = allowance + 65536
        self.remaining -= reserved
        data = read_bytes(self, relative, limit=allowance)
        self.remaining += reserved - len(data)
        return data

    def load_meta(self, session_id):
        session_id = validate_session_ref(session_id)
        if session_id not in self._records:
            self._records[session_id] = parse_json(self.read(Path(session_id) / "meta.json"))
        return self._records[session_id]

    def resolve_ref(self, reference: str) -> str:
        """Select an id/prefix/latest without following session directory links."""
        validate_session_ref(reference)
        directory = None
        names = []
        try:
            directory = _directory(self, ())
            with os.scandir(directory) as entries:
                for index, entry in enumerate(entries):
                    if index >= MAX_RECORD_FILES:
                        raise RecordError("session selection exceeds its file-count budget")
                    if entry.name == reference and not entry.is_dir(follow_symlinks=False):
                        raise RecordError("selected session directory is not a regular directory")
                    if entry.is_dir(follow_symlinks=False) and not entry.name.startswith("."):
                        names.append(entry.name)
        except OSError as exc:
            raise RecordError("selected session store is unavailable") from exc
        finally:
            if directory is not None:
                os.close(directory)
        if reference != "latest":
            matching = [name for name in names if name == reference]
            if not matching:
                matching = [name for name in names if name.startswith(reference)]
            if len(matching) != 1:
                raise RecordError("selected session reference is missing or ambiguous")
            meta = self.load_meta(matching[0])
            if meta.get("session_id") != matching[0]:
                raise RecordError("selected session identity conflicts with its directory")
            return matching[0]
        latest = None
        for name in names:
            try:
                meta = self.load_meta(name)
            except (RecordError, ValueError) as exc:
                if self.remaining <= 65536:
                    raise RecordError("session selection read budget exhausted") from exc
                continue
            timestamp = meta.get("updated_at")
            if meta.get("session_id") == name and isinstance(timestamp, str) \
                    and (latest is None or timestamp > latest[0]):
                latest = (timestamp, name)
        if latest is None:
            raise RecordError("no readable saved session was found")
        return latest[1]
