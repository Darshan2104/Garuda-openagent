"""Opt-in durable session metadata using the common meta.json.lock owner.

A registry outside the session directory preserves strict policy when local
markers or metadata disappear. These are cooperative integrity records, not
signatures against an actor controlling the entire store.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from garuda.runtime.private_records import FLAGS, open_directory, private_file, read_record
from garuda.runtime.strict_store import (
    CorruptRecord,
    StorageUnavailable,
    exclusive_lock,
    fcntl,
    preserve_private_backup,
    write_locked_document,
)

REGISTRY = ".publication-policies"
MARKER = ".publication-policy.json"
INITIALIZED = ".publication-initialized.json"
HEAD = ".publication-head.json"
META_LIMIT = 1_048_576


def _identity(fd):
    info = os.fstat(fd)
    return [info.st_dev, info.st_ino]


def _marker(policy):
    return {key: policy[key] for key in ("version", "kind", "session_id", "nonce")}


def _private_directory(fd):
    info = os.fstat(fd)
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise StorageUnavailable("publication directory is not owner-only")


def _child(parent, name):
    try:
        os.mkdir(name, 0o700, dir_fd=parent)
        os.fsync(parent)
    except FileExistsError:
        pass
    fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | FLAGS, dir_fd=parent)
    try:
        _private_directory(fd)
    except BaseException:
        os.close(fd)
        raise
    return fd


def protect_session(meta_path: Path):
    """Establish strict policy before creating the session's first metadata."""
    meta_path = meta_path.absolute()
    root = meta_path.parent.parent
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    root_fd = open_directory(root)
    directory = registry = lock = None
    try:
        registry = _child(root_fd, REGISTRY)
        directory = _child(root_fd, meta_path.parent.name)
        with exclusive_lock(root / REGISTRY) as registry_fd:
            existing = read_record(registry_fd, meta_path.parent.name + ".json")
            if existing is not None:
                raise StorageUnavailable("session publication policy already exists")
            lock = os.open("meta.json.lock", os.O_RDWR | os.O_CREAT | os.O_EXCL | FLAGS,
                           0o600, dir_fd=directory)
            if fcntl is None:
                raise StorageUnavailable("strict metadata requires cross-process locking")
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                os.stat("meta.json", dir_fd=directory, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise StorageUnavailable("cannot retrofit publication policy onto metadata")
            os.fsync(lock)
            os.fsync(directory)
            policy = {"version": 1, "kind": "flow-verification",
                      "session_id": meta_path.parent.name, "nonce": uuid.uuid4().hex,
                      "directory": _identity(directory), "lock": _identity(lock)}
            # A crash between registry and marker leaves a known protected ID;
            # a later writer refuses instead of falling back to legacy storage.
            preserve_private_backup(registry_fd, meta_path.parent.name + ".json",
                                    json.dumps(policy, sort_keys=True).encode())
            preserve_private_backup(directory, MARKER, json.dumps(_marker(policy), sort_keys=True).encode())
    finally:
        for fd in (lock, directory, registry, root_fd):
            if fd is not None:
                os.close(fd)


def _metadata(directory):
    fd = os.open("meta.json", os.O_RDONLY | FLAGS, dir_fd=directory)
    try:
        private_file(fd)
        with os.fdopen(os.dup(fd), "rb") as handle:
            raw = handle.read(META_LIMIT + 1)
        if len(raw) > META_LIMIT:
            raise CorruptRecord("session metadata exceeds its publication bound")

        def pairs(values):
            result = {}
            for key, value in values:
                if key in result:
                    raise CorruptRecord("duplicate session metadata field")
                result[key] = value
            return result

        def constant(_value):
            raise CorruptRecord("nonfinite session metadata value")

        document = json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)
        if not isinstance(document, dict):
            raise CorruptRecord("session metadata is not a mapping")
        return document, _identity(fd), hashlib.sha256(raw).hexdigest()
    except (ValueError, UnicodeError) as exc:
        raise CorruptRecord("session metadata is unreadable") from exc
    finally:
        os.close(fd)


def _policy(meta_path):
    # A legacy caller may address the same store through a directory symlink.
    # Resolve only the store root: protected IDs still find their policy and
    # fail strict traversal below, while unrelated legacy IDs keep working.
    registry = meta_path.parent.parent.resolve() / REGISTRY
    # Legacy stores may have symlinked ancestors or non-private directories.
    # Apply strict traversal only when a registry exists; local protection
    # evidence below still refuses a missing registry. Once observed, losing
    # the registry during traversal must fail rather than downgrade the cycle.
    if not os.path.lexists(registry):
        policy = None
    else:
        fd = open_directory(registry)
        try:
            _private_directory(fd)
            policy = read_record(fd, meta_path.parent.name + ".json")
        finally:
            os.close(fd)
    if policy is None:
        if os.path.lexists(meta_path.parent / MARKER) or os.path.lexists(meta_path.parent / INITIALIZED):
            raise StorageUnavailable("protected session lost its publication registry")
        # Independently recognize protection recorded in readable metadata.
        if meta_path.is_file() and not meta_path.is_symlink():
            with meta_path.open("rb") as handle:
                raw = handle.read(META_LIMIT + 1)
            try:
                document = json.loads(raw)
            except (ValueError, UnicodeError):
                document = None
            if isinstance(document, dict) and "publication_policy" in document:
                raise StorageUnavailable("protected metadata lost its publication registry")
        return None
    if (set(policy) != {"version", "kind", "session_id", "nonce", "directory", "lock"}
            or policy["kind"] != "flow-verification" or policy["session_id"] != meta_path.parent.name
            or not isinstance(policy["nonce"], str) or len(policy["nonce"]) != 32
            or any(not isinstance(policy[key], list) or len(policy[key]) != 2
                   or any(type(value) is not int or value < 0 for value in policy[key])
                   for key in ("directory", "lock"))):
        raise CorruptRecord("invalid session publication policy")
    return policy


class MetadataCycle:
    def __init__(self, path, policy, directory, lock, initializing):
        self.path, self.policy = path, policy
        self.directory, self.lock = directory, lock
        self.initializing = initializing
        self.head = None

    def validate(self):
        if (_identity(self.directory) != self.policy["directory"]
                or _identity(self.lock) != self.policy["lock"]
                or _policy(self.path) != self.policy
                or read_record(self.directory, MARKER) != _marker(self.policy)):
            raise StorageUnavailable("session publication ownership changed")
        info = os.stat("meta.json.lock", dir_fd=self.directory, follow_symlinks=False)
        current = self.path.parent.stat(follow_symlinks=False)
        if ([info.st_dev, info.st_ino] != self.policy["lock"]
                or [current.st_dev, current.st_ino] != self.policy["directory"]):
            raise StorageUnavailable("session directory or common metadata lock changed")

    def read(self):
        self.validate()
        stamp = read_record(self.directory, INITIALIZED)
        if self.initializing:
            if stamp is not None or os.path.lexists(self.path):
                raise StorageUnavailable("protected metadata was already initialized")
            preserve_private_backup(self.directory, INITIALIZED,
                                    json.dumps(_marker(self.policy), sort_keys=True).encode())
            return {}
        if stamp != _marker(self.policy):
            raise StorageUnavailable("session initialization evidence is unavailable")
        document, identity, digest = _metadata(self.directory)
        self.head = read_record(self.directory, HEAD)
        if (self.head is None or set(self.head) != {"version", "nonce", "metadata", "digest"}
                or self.head["nonce"] != self.policy["nonce"]
                or self.head["metadata"] != identity or self.head["digest"] != digest):
            raise CorruptRecord("session metadata differs from its durable publication head")
        if (document.get("publication_policy") != _marker(self.policy)
                or document.get("session_id") != self.policy["session_id"]):
            raise CorruptRecord("session metadata policy binding differs")
        return document

    def write(self, document):
        self.validate()
        if (read_record(self.directory, INITIALIZED) != _marker(self.policy)
                or read_record(self.directory, HEAD) != self.head):
            raise StorageUnavailable("session publication generation changed")
        if self.head is None:
            if os.path.lexists(self.path):
                raise StorageUnavailable("metadata appeared before initial publication")
        else:
            _current, identity, digest = _metadata(self.directory)
            if identity != self.head["metadata"] or digest != self.head["digest"]:
                raise StorageUnavailable("metadata changed during publication")
        if ("publication_policy" in document
                and document["publication_policy"] != _marker(self.policy)):
            raise CorruptRecord("cannot replace session publication policy")
        document = {**document, "publication_policy": _marker(self.policy)}
        if document.get("session_id") != self.policy["session_id"]:
            raise CorruptRecord("cannot replace session metadata identity")
        raw = json.dumps(document, indent=2, sort_keys=True, default=str, allow_nan=False).encode()
        if len(raw) > META_LIMIT:
            raise CorruptRecord("session metadata exceeds its publication bound")
        write_locked_document(self.directory, self.path, json.loads(raw))
        _published, identity, digest = _metadata(self.directory)
        self.head = {"version": 1, "nonce": self.policy["nonce"],
                     "metadata": identity, "digest": digest}
        write_locked_document(self.directory, self.path.parent / HEAD, self.head)
        self.validate()


@contextmanager
def publication_cycle(meta_path: Path, *, initializing=False):
    """Yield a strict transaction, or None for an ordinary legacy session."""
    meta_path = meta_path.absolute()
    policy = _policy(meta_path)
    if policy is None:
        yield None
        return
    if fcntl is None:
        raise StorageUnavailable("strict metadata requires cross-process locking")
    directory = open_directory(meta_path.parent)
    lock = None
    try:
        _private_directory(directory)
        lock = os.open("meta.json.lock", os.O_RDWR | FLAGS, dir_fd=directory)
        private_file(lock)
        cycle = MetadataCycle(meta_path, policy, directory, lock, initializing)
        cycle.validate()
        deadline = time.monotonic() + 5
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise StorageUnavailable("common metadata lock is unavailable") from None
                time.sleep(.01)
        cycle.validate()
        yield cycle
    finally:
        for fd in (lock, directory):
            if fd is not None:
                os.close(fd)
