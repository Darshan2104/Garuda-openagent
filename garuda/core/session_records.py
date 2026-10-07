"""Bounded read-only session records, anchored by no-follow descriptors."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

from garuda.core.sessions import SessionStore, validate_session_ref

MAX_RECORD_BYTES = 4 * 1024 * 1024
MAX_RECORD_FILES = 10_000


class RecordError(ValueError):
    pass


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

    try:
        result = json.loads(data, object_pairs_hook=unique, parse_constant=invalid_constant)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise RecordError('record is not valid unique-key JSON') from exc
    if not isinstance(result, dict):
        raise RecordError('record must be a mapping')
    return result


def read_json(store, relative):
    return parse_json(read_bytes(store, relative))


def list_json(store, relative):
    directory = None
    try:
        directory = _directory(store, _parts(relative))
        names = []
        with os.scandir(directory) as entries:
            for entry in entries:
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
