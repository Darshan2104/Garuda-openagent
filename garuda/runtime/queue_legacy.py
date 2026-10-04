"""Read prototype queues for diagnosis; migrate only proved empty records.

Legacy jobs have no user/session/configuration bindings and cannot be imported.
An empty record is archived byte-for-byte before publication, through the same
locked directory descriptor. Existing archives are verified, never overwritten.
"""

from __future__ import annotations

import json
import os
import stat
import uuid
from pathlib import Path


class LegacyQueueError(ValueError):
    """Legacy migration cannot be proved safe; preserve records for diagnosis."""


def _integer(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def project(document: dict) -> dict:
    """A validated diagnostic projection, without inventing admission authority."""
    scopes = document.get("scopes")
    if (type(document.get("version")) is not int or document["version"] != 1
            or not _integer(document.get("seq")) or not isinstance(scopes, dict)
            or set(document) != {"version", "seq", "scopes"}):
        raise LegacyQueueError("legacy queue record is partial or malformed; refusing")
    projected = {}
    for scope, entry in scopes.items():
        if (not isinstance(scope, str) or not isinstance(entry, dict)
                or set(entry) != {"capacity", "waiting", "claims"}
                or not _integer(entry.get("capacity")) or entry["capacity"] < 1
                or not isinstance(entry.get("waiting"), list)
                or not isinstance(entry.get("claims"), dict)):
            raise LegacyQueueError("legacy queue scope is partial or malformed; refusing")
        waiting, claims = entry["waiting"], entry["claims"]
        if (not all(isinstance(w, dict) and isinstance(w.get("id"), str)
                    and _integer(w.get("seq")) for w in waiting)
                or not all(isinstance(k, str) and isinstance(v, dict) for k, v in claims.items())):
            raise LegacyQueueError("legacy queue entries are partial or malformed; refusing")
        harness = scope.rsplit(":", 1)[-1]
        projected[scope] = {
            "waiting": [{"id": w["id"], "seq": w["seq"], "user": None,
                         "harness": harness, "session_id": None, "config_digest": None,
                         "enqueued_at": None} for w in waiting],
            "claims": {k: {**v, "user": None, "harness": harness, "session_id": None,
                           "config_digest": None, "claimed_at": v.get("heartbeat")}
                       for k, v in claims.items()},
        }
    return {"version": 2, "seq": document["seq"], "scopes": projected}


def _read_private(directory_fd: int, name: str, *, durable: bool = False) -> bytes:
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600):
            raise LegacyQueueError(f"legacy source/backup {name} is not an owner-only regular file")
        raw = handle.read()
        if durable:
            os.fsync(handle.fileno())
        return raw


def preserve_empty(directory_fd: int, expected: dict) -> None:
    """Archive a safe empty legacy record, durably, before any mutation."""
    if any(s["waiting"] or s["claims"] for s in expected["scopes"].values()):
        raise LegacyQueueError(
            "legacy queue contains work without user/session/configuration bindings; "
            "refusing migration even for dead owners. Inspect the original state.json "
            "and resolve work with the prior version; its bytes remain available"
        )
    try:
        raw = _read_private(directory_fd, "state.json")
        if project(json.loads(raw)) != expected:
            raise LegacyQueueError("legacy source changed before backup; refusing")
        try:
            fd = os.open("state.json.v1", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o600, dir_fd=directory_fd)
        except FileExistsError:
            if _read_private(directory_fd, "state.json.v1", durable=True) != raw:
                raise LegacyQueueError(
                    "legacy backup differs from source; preserve it for diagnosis"
                ) from None
        else:
            with os.fdopen(fd, "wb") as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
        os.fsync(directory_fd)
    except (OSError, ValueError) as exc:
        raise LegacyQueueError(f"cannot preserve legacy source/backup: {exc}; refusing") from exc


def publish(directory_fd: int, root: Path, document: dict) -> None:
    """Publish through the archived source's locked directory, never a symlink target."""
    temporary = f".state.migration.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    try:
        bound = os.fstat(directory_fd)
        current = root.stat(follow_symlinks=False)
        if (current.st_dev, current.st_ino) != (bound.st_dev, bound.st_ino):
            raise LegacyQueueError("legacy queue directory changed before publication; refusing")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600, dir_fd=directory_fd)
        with os.fdopen(fd, "wb") as handle:
            handle.write(json.dumps(document, indent=2, sort_keys=True).encode())
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, "state.json", src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
        os.fsync(directory_fd)
    except OSError as exc:
        raise LegacyQueueError(f"legacy publication failed: {exc}; preserved artifacts remain") from exc
