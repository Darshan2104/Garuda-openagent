"""Bounded, best-effort observations separate from authoritative ASK records."""

import json
import os
import stat
from pathlib import Path

from garuda.runtime.strict_store import exclusive_lock

MAX_SEGMENT_BYTES = 1024 * 1024
MAX_RECORD_BYTES = 4096


def append_decision(directory: Path, record: dict) -> None:
    """Append under the session lock, retaining at most two 1 MiB segments.

    Callers run this host-side I/O off the event loop. Rotation drops old
    observations; this trail never grants authority or claims complete history.
    """
    data = (json.dumps(record, separators=(",", ":"), ensure_ascii=True) + "\n").encode()
    if len(data) > MAX_RECORD_BYTES:
        raise ValueError("decision audit record exceeds its byte limit")
    with exclusive_lock(directory) as directory_fd:
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK
        fd = os.open("current.jsonl", flags, 0o600, dir_fd=directory_fd)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise OSError("decision audit is not a regular file")
            if info.st_size + len(data) > MAX_SEGMENT_BYTES:
                os.replace("current.jsonl", "previous.jsonl",
                           src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
                os.close(fd)
                fd = -1
                fd = os.open("current.jsonl", flags | os.O_EXCL, 0o600, dir_fd=directory_fd)
            with os.fdopen(fd, "ab", closefd=False) as stream:
                stream.write(data)
                stream.flush()
        finally:
            if fd >= 0:
                os.close(fd)
