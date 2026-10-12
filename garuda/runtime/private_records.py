"""Bounded descriptor reads for private cooperative ownership records.

These helpers check containment, file type, ownership, permissions and schema
version. Reading a record does not grant the authority it describes.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

from garuda.runtime.strict_store import CorruptRecord, StorageUnavailable

LIMIT = 65_536
FLAGS = os.O_NOFOLLOW | os.O_NONBLOCK


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise CorruptRecord("duplicate record field")
        result[key] = value
    return result


def _constant(value):
    raise CorruptRecord("nonfinite JSON number")


def private_file(fd: int) -> None:
    info = os.fstat(fd)
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600):
        raise StorageUnavailable("record is not an owner-only regular file")


def open_directory(path: Path) -> int:
    """Open every ancestor without following links or trusting writable parents."""
    if not path.is_absolute() or ".." in path.parts:
        raise StorageUnavailable("private directory must have an absolute contained path")
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | FLAGS, dir_fd=fd)
            os.close(fd)
            fd = next_fd
            info = os.fstat(fd)
            shared_tmp = info.st_uid == 0 and bool(info.st_mode & stat.S_ISVTX)
            if info.st_uid not in (0, os.getuid()) or (info.st_mode & 0o022 and not shared_tmp):
                raise StorageUnavailable("private directory has an untrusted ancestor")
        return fd
    except BaseException:
        os.close(fd)
        raise


def read_record(directory: int, name: str, *, versions=(1,)) -> dict | None:
    try:
        fd = os.open(name, os.O_RDONLY | FLAGS, dir_fd=directory)
    except FileNotFoundError:
        return None
    try:
        private_file(fd)
        with os.fdopen(os.dup(fd), "rb") as handle:
            raw = handle.read(LIMIT + 1)
        if len(raw) > LIMIT:
            raise CorruptRecord("check record exceeds its bound")
        try:
            document = json.loads(raw, object_pairs_hook=_pairs, parse_constant=_constant)
        except (ValueError, UnicodeError) as exc:
            raise CorruptRecord("unreadable check record") from exc
        if (not isinstance(document, dict) or type(document.get("version")) is not int
                or document["version"] not in versions):
            raise CorruptRecord("unsupported check record")
        return document
    finally:
        os.close(fd)
