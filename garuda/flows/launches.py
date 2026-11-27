"""Per-attempt launch records: which session a launched step attempt runs as.

A step attempt's intent is journaled before anything launches, so it cannot
name the step session, which the launcher creates. Once that session id is
known the launcher writes ``flow/launches/<step>-<attempt>.json`` beside the
receipts. It links a flow to its in-flight step session before the receipt
exists (``flow show``'s live status, the dashboard), and, for a checked step,
it also records the owned worker's Linux subreaper supervisor, the worker
command and the parent lease owner, each with its start identity, so a
``flow resume`` after a hard crash can prove the attempt's whole process tree
gone (:mod:`garuda.flows.crash`).

The journal keeps exactly two entries per attempt; this record is separate
and additive. Writing it is best effort: a missing or unreadable record never
stops a step, it only means nothing can be proved from it.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

DIRECTORY = "launches"
#: Bound on one record; it holds a few identities and ids.
MAX_BYTES = 16 * 1024


def path(flow_dir, step: str, attempt: int) -> Path:
    return Path(flow_dir) / DIRECTORY / f"{step}-{attempt}.json"


def read(flow_dir, step: str, attempt: int) -> dict | None:
    """The attempt's launch record, or None when it is missing or unreadable."""
    try:
        fd = os.open(path(flow_dir, step, attempt), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "rb") as handle:
            raw = handle.read(MAX_BYTES + 1)
        record = json.loads(raw) if len(raw) <= MAX_BYTES else None
    except (OSError, ValueError):
        return None
    if (not isinstance(record, dict) or record.get("step") != step
            or record.get("attempt") != attempt):
        return None
    return record


def write(flow_dir, step: str, attempt: int, fields: dict) -> bool:
    """Merge ``fields`` into the attempt's record, atomically and durably. Best effort."""
    if not isinstance(step, str) or not step or "/" in step or type(attempt) is not int:
        return False
    target = path(flow_dir, step, attempt)
    if not target.parent.parent.is_dir():
        return False  # no flow directory: a launcher used outside a flow (tests)
    try:
        target.parent.mkdir(mode=0o700, exist_ok=True)
        record = {**(read(flow_dir, step, attempt) or {}), **fields,
                  "version": 1, "step": step, "attempt": attempt}
        raw = json.dumps(record, sort_keys=True).encode()
        if len(raw) > MAX_BYTES:
            return False
        temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC
                     | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            os.write(fd, raw)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(temporary, target)
        directory = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        return True
    except OSError:
        return False
