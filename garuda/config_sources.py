"""Capture source identities at the actual parse read, scoped to one preparation."""

from __future__ import annotations

import hashlib
import os
import stat
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path

MAX_SOURCE_BYTES = 4 * 1024 * 1024
MAX_VERIFIED_SOURCE_BYTES = 16 * 1024 * 1024
_ACTIVE = ContextVar("garuda_source_capture", default=None)


class SourceChanged(ValueError):
    """An admitted source changed or its original read was ambiguous."""


@dataclass(frozen=True)
class SourceFact:
    path: str
    digest: str | None
    max_bytes: int = MAX_SOURCE_BYTES


class SourceCapture:
    """No raw configuration is retained; repeated reads must agree."""

    def __init__(self):
        self._facts = {}
        self._invalid = False

    def __enter__(self):
        self._token = _ACTIVE.set(self)
        return self

    def __exit__(self, *_):
        _ACTIVE.reset(self._token)

    def observe(self, path, digest, *, max_bytes=MAX_SOURCE_BYTES):
        name = os.path.abspath(Path(path).expanduser())
        if (type(max_bytes) is not int or max_bytes <= 0
                or max_bytes > MAX_VERIFIED_SOURCE_BYTES):
            self._invalid = True
            return
        prior = self._facts.get(name)
        if prior is not None and prior[0] != digest:
            self._invalid = True
        self._facts[name] = (digest, min(prior[1], max_bytes) if prior else max_bytes)

    def facts(self):
        if self._invalid or len(self._facts) > 256:
            raise SourceChanged("source preparation was inconsistent or unbounded")
        return tuple(SourceFact(path, digest, max_bytes)
                     for path, (digest, max_bytes) in sorted(self._facts.items()))


def record_source(path, raw):
    capture = _ACTIVE.get()
    if capture is not None:
        if raw is not None and len(raw) > MAX_SOURCE_BYTES:
            capture._invalid = True
        capture.observe(path, hashlib.sha256(raw).hexdigest() if raw is not None else None)


def record_digest(path, digest, *, max_bytes=MAX_SOURCE_BYTES):
    capture = _ACTIVE.get()
    if capture is not None:
        capture.observe(path, digest, max_bytes=max_bytes)


def record_source_error():
    capture = _ACTIVE.get()
    if capture is not None:
        capture._invalid = True


def source_is_file(path):
    exists = path.is_file()
    if not exists:
        record_source(path, None)
    return exists


def read_source_fd(path, descriptor, legacy_limit):
    """Hash the same descriptor bytes the parser consumes, before truncation."""
    if _ACTIVE.get() is None:
        return os.read(descriptor, legacy_limit)
    chunks, size = [], 0
    while size <= MAX_SOURCE_BYTES:
        raw = os.read(descriptor, min(65_536, MAX_SOURCE_BYTES + 1 - size))
        if not raw:
            break
        chunks.append(raw)
        size += len(raw)
    value = b"".join(chunks)
    record_source(path, value)
    return value


def read_source_text(path):
    try:
        if _ACTIVE.get() is None:
            raw = path.read_bytes()
        else:
            with path.open("rb") as handle:
                raw = handle.read(MAX_SOURCE_BYTES + 1)
            if len(raw) > MAX_SOURCE_BYTES:
                record_source_error()
                raise SourceChanged("source exceeds the preparation bound")
    except FileNotFoundError:
        record_source(path, None)
        raise
    record_source(path, raw)
    try:
        return raw.decode("utf-8")
    except UnicodeError:
        record_source_error()
        raise


def verify_sources(documents):
    """Bounded no-follow validation, run only inside an owned read worker."""
    from garuda.runtime.private_records import FLAGS, open_directory

    if not isinstance(documents, list) or len(documents) > 256:
        raise SourceChanged("invalid source snapshot")
    for document in documents:
        if (not isinstance(document, dict)
                or set(document) not in ({"path", "digest"},
                                         {"path", "digest", "max_bytes"})):
            raise SourceChanged("invalid source identity")
        path = Path(document["path"])
        expected = document["digest"]
        max_bytes = document.get("max_bytes", MAX_SOURCE_BYTES)
        if expected is not None and (not isinstance(expected, str) or len(expected) != 64):
            raise SourceChanged("invalid source digest")
        if (type(max_bytes) is not int or max_bytes <= 0
                or max_bytes > MAX_VERIFIED_SOURCE_BYTES):
            raise SourceChanged("invalid source size bound")
        directory = descriptor = None
        try:
            directory = open_directory(path.parent)
            descriptor = os.open(path.name, os.O_RDONLY | FLAGS, dir_fd=directory)
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_size > max_bytes:
                raise SourceChanged("source is not a bounded regular file")
            with os.fdopen(os.dup(descriptor), "rb") as handle:
                raw = handle.read(max_bytes + 1)
            if len(raw) > max_bytes or hashlib.sha256(raw).hexdigest() != expected:
                raise SourceChanged("admitted source changed")
        except FileNotFoundError:
            if expected is not None:
                raise SourceChanged("admitted source disappeared") from None
        finally:
            for fd in (descriptor, directory):
                if fd is not None:
                    os.close(fd)
    return {"valid": True}
