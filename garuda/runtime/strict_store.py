"""Strict local storage for ownership records (plan task B.0, issue #157).

Every record Garuda uses to decide who may mutate something — workspace leases,
capacity reservations, queue state — goes through these helpers:

- the directory is owner-only (``0700``) and may not be a symlink;
- an exclusive ``fcntl.flock`` on ``<dir>/.lock`` (opened without following a
  symlink) serializes read-check-write cycles across processes; if it cannot
  be taken the caller gets :class:`StorageUnavailable` and nothing is written;
- documents are read without following symlinks; unreadable, malformed or
  future-version documents raise :class:`CorruptRecord` and are left in place
  for diagnosis;
- writes go to a temporary file, are fsynced, renamed over the old document,
  and the directory is fsynced, so a crash leaves either the old or the new
  version, never a partial one. Files are ``0600``.

POSIX only: without ``fcntl`` every mutation refuses.
"""

from __future__ import annotations

import contextlib
import errno
import json
import os
import uuid
from collections.abc import Iterator
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows has no fcntl
    fcntl = None  # type: ignore[assignment]

_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


class StorageError(Exception):
    """Base error for strict storage."""


class StorageUnavailable(StorageError):
    """The store cannot be locked or created safely; nothing was changed."""


class CorruptRecord(StorageError):
    """A record is unreadable, malformed or from a future version."""


def ensure_private_dir(directory: Path) -> None:
    if directory.is_symlink():
        raise StorageUnavailable(f"{directory} is a symlink; refusing to use it")
    try:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(directory, 0o700)
    except OSError as exc:
        raise StorageUnavailable(f"cannot create {directory}: {exc}") from exc


@contextlib.contextmanager
def exclusive_lock(directory: Path) -> Iterator[None]:
    """Hold the directory's cross-process lock; refuse rather than run unlocked."""
    if fcntl is None:
        raise StorageUnavailable("cross-process locking is unavailable on this platform")
    ensure_private_dir(directory)
    try:
        fd = os.open(directory / ".lock", os.O_RDWR | os.O_CREAT | _NOFOLLOW, 0o600)
    except OSError as exc:
        raise StorageUnavailable(f"cannot open the lock in {directory}: {exc}") from exc
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
        except OSError as exc:
            raise StorageUnavailable(f"cannot lock {directory}: {exc}") from exc
        yield
    finally:
        os.close(fd)


def read_document(path: Path, *, versions: tuple[int, ...]) -> dict | None:
    """The JSON document at ``path``, or ``None`` when it does not exist.

    ``versions`` are the schema versions this reader understands; a missing
    ``version`` counts as 1 (records written before versions were kept).
    """
    try:
        fd = os.open(path, os.O_RDONLY | _NOFOLLOW)
    except FileNotFoundError:
        return None
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise CorruptRecord(f"{path} is a symlink; refusing") from exc
        raise CorruptRecord(f"cannot read {path}: {exc}; refusing") from exc
    try:
        with os.fdopen(fd, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError) as exc:
        raise CorruptRecord(f"unreadable record at {path}: {exc}; refusing") from exc
    if not isinstance(data, dict):
        raise CorruptRecord(f"record at {path} is not a mapping; refusing")
    version = data.get("version", 1)
    if version not in versions:
        raise CorruptRecord(
            f"record at {path} has version {version!r}; this Garuda reads {versions}; refusing"
        )
    return data


def write_document(path: Path, document: dict) -> None:
    """Replace ``path`` atomically and durably with ``document``."""
    ensure_private_dir(path.parent)
    tmp = path.parent / f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW, 0o600)
    try:
        os.write(fd, json.dumps(document, indent=2, sort_keys=True).encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    try:
        os.replace(tmp, path)
    except OSError:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    dir_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def remove_document(path: Path) -> None:
    with contextlib.suppress(FileNotFoundError):
        os.unlink(path)
    dir_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)
