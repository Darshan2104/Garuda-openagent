"""Private, at-most-once operation records for a future owned flow gateway.

This store records identity and lifecycle facts; reading or advancing an intent
does not launch, resume, reclaim a lease, or prove descendant closure. Callers
must perform those checks at their respective execution owners. In particular,
an uncertain transition is sticky rather than permission to retry a launch.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import stat
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

from garuda.runtime.ownership import Owner, owner_liveness
from garuda.runtime.strict_store import CorruptRecord, StorageUnavailable

try:
    import fcntl
except ImportError:  # pragma: no cover - POSIX-only storage boundary
    fcntl = None  # type: ignore[assignment]

_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_MAX_BYTES = 64 * 1024
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_EPOCH = re.compile(r"[0-9a-f]{32}\Z")
_REASON = re.compile(r"[a-z][a-z0-9_.-]{0,79}\Z")
_PHASES = {
    "prepared": frozenset({"launch-authorized", "refused", "abandoned", "uncertain"}),
    "launch-authorized": frozenset({"admitted", "uncertain", "quarantined"}),
    "admitted": frozenset({"terminal", "uncertain", "quarantined"}),
    "terminal": frozenset(),
    "refused": frozenset(),
    "abandoned": frozenset(),
    "uncertain": frozenset(),
    "quarantined": frozenset(),
}
_FIELDS = frozenset({
    "version", "operation_id", "request_digest", "plan_digest", "authority_digest",
    "workspace", "workspace_identity", "store_identity", "compiler_version",
    "catalog_version", "reserved_session", "owner", "sequence", "phase",
    "session_id", "lease_epoch", "outcome", "reason",
})
_IDENTITY = frozenset({
    "operation_id", "request_digest", "plan_digest", "authority_digest", "workspace",
    "workspace_identity", "store_identity", "compiler_version", "catalog_version",
})


class IntentConflict(Exception):
    """An operation ID was reused for different immutable launch inputs."""


class IntentStateError(Exception):
    """The requested lifecycle transition is stale or unauthorized."""


def _uuid4(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError):
        return False
    return parsed.version == 4 and str(parsed) == value


def _identity_pair(value: object) -> bool:
    return (type(value) is list and len(value) == 2
            and all(type(part) is int and part >= 0 for part in value))


def _owner(value: object) -> bool:
    return (type(value) is dict and set(value) == {"pid", "identity", "pgid", "epoch"}
            and type(value["pid"]) is int and value["pid"] > 0
            and type(value["pgid"]) is int and value["pgid"] > 0
            and isinstance(value["identity"], str)
            and value["identity"] not in ("", "unknown")
            and isinstance(value["epoch"], str) and _EPOCH.fullmatch(value["epoch"]) is not None)


def _validate(record: object) -> dict:
    if type(record) is not dict or set(record) != _FIELDS:
        raise CorruptRecord("launch intent has unknown or missing fields")
    if type(record["version"]) is not int or record["version"] != 1:
        raise CorruptRecord("launch intent version is unsupported")
    if not _uuid4(record["operation_id"]) or not _uuid4(record["reserved_session"]):
        raise CorruptRecord("launch intent has an invalid operation or session ID")
    if any(type(record[key]) is not str or _DIGEST.fullmatch(record[key]) is None
           for key in ("request_digest", "plan_digest", "authority_digest")):
        raise CorruptRecord("launch intent has an invalid digest")
    workspace = record["workspace"]
    if (type(workspace) is not str or not workspace.startswith("/")
            or str(Path(workspace)) != workspace or ".." in Path(workspace).parts):
        raise CorruptRecord("launch intent workspace is not canonical")
    if not _identity_pair(record["workspace_identity"]) or not _identity_pair(record["store_identity"]):
        raise CorruptRecord("launch intent has an invalid filesystem identity")
    if (type(record["compiler_version"]) is not int or record["compiler_version"] < 1
            or type(record["catalog_version"]) is not int or record["catalog_version"] < 1):
        raise CorruptRecord("launch intent has an invalid compiler or catalog version")
    if not _owner(record["owner"]):
        raise CorruptRecord("launch intent has an invalid owner")
    if type(record["sequence"]) is not int or record["sequence"] < 0:
        raise CorruptRecord("launch intent has an invalid sequence")
    phase = record["phase"]
    if type(phase) is not str or phase not in _PHASES:
        raise CorruptRecord("launch intent has an invalid phase")
    if ((phase == "prepared" and record["sequence"] != 0)
            or (phase == "admitted" and record["sequence"] < 2)
            or (phase == "terminal" and record["sequence"] < 3)
            or (phase not in {"prepared", "admitted", "terminal"}
                and record["sequence"] < 1)):
        raise CorruptRecord("launch intent sequence disagrees with its phase")
    session, epoch, outcome, reason = (record[key] for key in
                                        ("session_id", "lease_epoch", "outcome", "reason"))
    if session is not None and (not _uuid4(session) or session != record["reserved_session"]):
        raise CorruptRecord("launch intent session binding disagrees with its reservation")
    if epoch is not None and (type(epoch) is not str or _EPOCH.fullmatch(epoch) is None):
        raise CorruptRecord("launch intent has an invalid lease epoch")
    if outcome is not None and (type(outcome) is not str
                                or outcome not in {"completed", "stopped", "interrupted"}):
        raise CorruptRecord("launch intent has an invalid outcome")
    if reason is not None and (type(reason) is not str or _REASON.fullmatch(reason) is None):
        raise CorruptRecord("launch intent has an invalid reason code")
    if phase in {"prepared", "launch-authorized", "refused", "abandoned"} and (session or epoch or outcome):
        raise CorruptRecord("launch intent has a binding before admission")
    if phase in {"admitted", "terminal"} and (session is None or epoch is None):
        raise CorruptRecord("launch intent is missing its admitted binding")
    if phase == "terminal" and outcome is None:
        raise CorruptRecord("terminal launch intent has no outcome")
    if phase != "terminal" and outcome is not None:
        raise CorruptRecord("nonterminal launch intent has an outcome")
    if phase in {"refused", "abandoned", "uncertain", "quarantined"} and reason is None:
        raise CorruptRecord("refused or uncertain launch intent has no reason")
    if phase not in {"refused", "abandoned", "uncertain", "quarantined"} and reason is not None:
        raise CorruptRecord("active launch intent has a refusal reason")
    return record


def _unique_pairs(pairs: list[tuple[str, object]]) -> dict:
    out = {}
    for key, value in pairs:
        if key in out:
            raise ValueError("duplicate JSON field")
        out[key] = value
    return out


class LaunchIntentStore:
    """Descriptor-bound private records beneath an existing session-store root."""

    def __init__(self, sessions_root: str | Path):
        self.root = Path(sessions_root)

    @contextlib.contextmanager
    def _locked(self, *, create: bool = False) -> Iterator[tuple[int | None, list[int]]]:
        if fcntl is None:
            raise StorageUnavailable("launch intents require POSIX cross-process locking")
        if not self.root.is_absolute() or ".." in self.root.parts:
            raise StorageUnavailable("session store root must be an absolute, canonical path")
        fd = None
        root_fd = None
        intents_fd = None
        lock_fd = None
        try:
            fd = os.open("/", os.O_RDONLY | _DIRECTORY)
            for part in self.root.parts[1:]:
                next_fd = os.open(part, os.O_RDONLY | _DIRECTORY | _NOFOLLOW,
                                  dir_fd=fd)
                info = os.fstat(next_fd)
                if not stat.S_ISDIR(info.st_mode):
                    raise StorageUnavailable("session store path has a non-directory component")
                os.close(fd)
                fd = next_fd
            root_fd, fd = fd, None
            root_info = os.fstat(root_fd)
            if root_info.st_uid != os.getuid() or stat.S_IMODE(root_info.st_mode) & 0o022:
                raise StorageUnavailable("session store root is not privately owned")
            made_dir = False
            if create:
                try:
                    os.mkdir("launch-intents", mode=0o700, dir_fd=root_fd)
                    os.fsync(root_fd)
                    made_dir = True
                except FileExistsError:
                    pass
            try:
                intents_fd = os.open("launch-intents", os.O_RDONLY | _DIRECTORY | _NOFOLLOW,
                                     dir_fd=root_fd)
            except FileNotFoundError:
                if not create:
                    yield None, [root_info.st_dev, root_info.st_ino]
                    return
                raise
            info = os.fstat(intents_fd)
            if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) != 0o700):
                raise StorageUnavailable("launch intent directory is not owner-only")
            current = os.stat("launch-intents", dir_fd=root_fd, follow_symlinks=False)
            if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
                raise StorageUnavailable("launch intent directory changed")
            if made_dir:
                lock_fd = os.open(".lock", os.O_RDWR | os.O_CREAT | os.O_EXCL | _NOFOLLOW,
                                  0o600, dir_fd=intents_fd)
                os.fsync(intents_fd)
            else:
                # Another process may have just created the directory but not
                # yet fsynced its lock. Never recreate a missing lock in an
                # existing directory: it could fence a different inode.
                deadline = time.monotonic() + 5.0
                while True:
                    try:
                        lock_fd = os.open(".lock", os.O_RDWR | os.O_NONBLOCK | _NOFOLLOW,
                                          dir_fd=intents_fd)
                        break
                    except FileNotFoundError:
                        if not create or time.monotonic() >= deadline:
                            raise StorageUnavailable("launch intent lock is missing") from None
                        time.sleep(0.01)
            lock_info = os.fstat(lock_fd)
            if (not stat.S_ISREG(lock_info.st_mode) or lock_info.st_nlink != 1
                    or lock_info.st_uid != os.getuid()
                    or stat.S_IMODE(lock_info.st_mode) != 0o600):
                raise StorageUnavailable("launch intent lock is not an owner-only regular file")
            deadline = time.monotonic() + 5.0
            while True:
                try:
                    fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError as exc:
                    if time.monotonic() >= deadline:
                        raise StorageUnavailable("launch intent lock is busy") from exc
                    time.sleep(0.01)
            current_lock = os.stat(".lock", dir_fd=intents_fd, follow_symlinks=False)
            if (current_lock.st_dev, current_lock.st_ino) != (lock_info.st_dev, lock_info.st_ino):
                raise StorageUnavailable("launch intent lock changed after acquisition")
            yield intents_fd, [root_info.st_dev, root_info.st_ino]
            current_dir = os.stat("launch-intents", dir_fd=root_fd, follow_symlinks=False)
            current_root = os.stat(self.root, follow_symlinks=False)
            current_lock = os.stat(".lock", dir_fd=intents_fd, follow_symlinks=False)
            if ((current_root.st_dev, current_root.st_ino) != (root_info.st_dev, root_info.st_ino)
                    or (current_dir.st_dev, current_dir.st_ino) != (info.st_dev, info.st_ino)
                    or (current_lock.st_dev, current_lock.st_ino)
                    != (lock_info.st_dev, lock_info.st_ino)):
                raise StorageUnavailable("launch intent store changed during access")
        except OSError as exc:
            raise StorageUnavailable(f"launch intent store is unavailable: {exc}") from exc
        finally:
            for held in (lock_fd, intents_fd, root_fd, fd):
                if held is not None:
                    os.close(held)

    @staticmethod
    def _read(directory_fd: int, operation_id: str) -> dict | None:
        name = f"{operation_id}.json"
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NONBLOCK | _NOFOLLOW,
                         dir_fd=directory_fd)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise CorruptRecord(f"cannot open launch intent: {exc}") from exc
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                    or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600):
                raise CorruptRecord("launch intent is not an owner-only regular file")
            if info.st_size > _MAX_BYTES:
                raise CorruptRecord("launch intent exceeds 64 KiB")
            chunks = []
            remaining = _MAX_BYTES + 1
            while remaining:
                chunk = os.read(fd, remaining)
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            raw = b"".join(chunks)
            if len(raw) > _MAX_BYTES:
                raise CorruptRecord("launch intent exceeds 64 KiB")
            try:
                record = _validate(json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_pairs))
                if record["operation_id"] != operation_id:
                    raise CorruptRecord("launch intent filename and operation ID disagree")
                return record
            except (UnicodeError, ValueError, TypeError) as exc:
                raise CorruptRecord("launch intent JSON is invalid or has duplicate fields") from exc
        finally:
            os.close(fd)

    @staticmethod
    def _write(directory_fd: int, operation_id: str, record: dict) -> None:
        encoded = json.dumps(_validate(record), sort_keys=True, separators=(",", ":")).encode()
        if len(encoded) > _MAX_BYTES:
            raise StorageUnavailable("launch intent exceeds 64 KiB")
        temporary = f".{operation_id}.{uuid.uuid4().hex}.tmp"
        fd = None
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW,
                         0o600, dir_fd=directory_fd)
            remaining = memoryview(encoded)
            while remaining:
                written = os.write(fd, remaining)
                if written <= 0:
                    raise StorageUnavailable("launch intent write made no progress")
                remaining = remaining[written:]
            os.fsync(fd)
            os.close(fd)
            fd = None
            os.replace(temporary, f"{operation_id}.json", src_dir_fd=directory_fd,
                       dst_dir_fd=directory_fd)
            os.fsync(directory_fd)
        except OSError as exc:
            # A rename may have succeeded before the directory fsync failed. The
            # caller must inspect the operation, never assume this was rolled back.
            raise StorageUnavailable(f"cannot publish launch intent: {exc}") from exc
        finally:
            if fd is not None:
                os.close(fd)

    def read(self, operation_id: str) -> dict | None:
        if not _uuid4(operation_id):
            raise ValueError("operation_id must be a canonical UUIDv4")
        with self._locked() as (directory_fd, store_identity):
            if directory_fd is None:
                return None
            record = self._read(directory_fd, operation_id)
            if record is not None and record["store_identity"] != store_identity:
                raise CorruptRecord("launch intent belongs to another session store")
            return record

    def prepare(self, *, operation_id: str, request_digest: str, plan_digest: str,
                authority_digest: str, workspace: str, workspace_identity: list[int],
                compiler_version: int, catalog_version: int, owner: Owner) -> tuple[dict, bool]:
        if (not isinstance(owner, Owner) or not _owner(owner.to_dict())
                or owner.pid != os.getpid() or owner_liveness(owner.to_dict()) is not True):
            raise IntentStateError("launch intent owner identity is not live and proven")
        with self._locked(create=True) as (directory_fd, store_identity):
            assert directory_fd is not None
            candidate = {
                "version": 1, "operation_id": operation_id,
                "request_digest": request_digest, "plan_digest": plan_digest,
                "authority_digest": authority_digest, "workspace": workspace,
                "workspace_identity": workspace_identity, "store_identity": store_identity,
                "compiler_version": compiler_version, "catalog_version": catalog_version,
                "reserved_session": str(uuid.uuid4()), "owner": owner.to_dict(),
                "sequence": 0, "phase": "prepared", "session_id": None,
                "lease_epoch": None, "outcome": None, "reason": None,
            }
            _validate(candidate)
            existing = self._read(directory_fd, operation_id)
            if existing is not None:
                if existing["store_identity"] != store_identity:
                    raise CorruptRecord("launch intent belongs to another session store")
                if any(existing[key] != candidate[key] for key in _IDENTITY):
                    raise IntentConflict("operation ID already names another launch request")
                return existing, False
            self._write(directory_fd, operation_id, candidate)
            return candidate, True

    def advance(self, operation_id: str, *, expected: str, phase: str, owner: Owner,
                session_id: str | None = None, lease_epoch: str | None = None,
                outcome: str | None = None, reason: str | None = None) -> dict:
        if not _uuid4(operation_id):
            raise ValueError("operation_id must be a canonical UUIDv4")
        if (not isinstance(owner, Owner) or not _owner(owner.to_dict())
                or owner.pid != os.getpid() or owner_liveness(owner.to_dict()) is not True):
            raise IntentStateError("launch intent owner identity is not live and proven")
        with self._locked() as (directory_fd, store_identity):
            if directory_fd is None:
                raise IntentStateError("launch intent is missing")
            record = self._read(directory_fd, operation_id)
            if record is None:
                raise IntentStateError("launch intent is missing")
            if record["store_identity"] != store_identity:
                raise CorruptRecord("launch intent belongs to another session store")
            if record["owner"] != owner.to_dict():
                raise IntentStateError("launch intent belongs to another owner")
            if record["phase"] != expected or phase not in _PHASES[record["phase"]]:
                raise IntentStateError("launch intent transition is stale or illegal")
            updated = {**record, "phase": phase, "sequence": record["sequence"] + 1}
            if phase == "admitted":
                updated["session_id"], updated["lease_epoch"] = session_id, lease_epoch
            elif session_id is not None or lease_epoch is not None:
                raise IntentStateError("only admission may establish a session binding")
            if phase == "terminal":
                updated["outcome"] = outcome
            elif outcome is not None:
                raise IntentStateError("only terminal may record an outcome")
            updated["reason"] = reason
            try:
                _validate(updated)
            except CorruptRecord as exc:
                raise IntentStateError(str(exc)) from exc
            self._write(directory_fd, operation_id, updated)
            return updated
