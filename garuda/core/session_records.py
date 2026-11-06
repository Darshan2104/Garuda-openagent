"""Bounded read-only session records, anchored by no-follow descriptors."""

from __future__ import annotations

import hashlib
import json
import math
import os
import stat
from contextlib import contextmanager
from pathlib import Path
from typing import Protocol

from garuda.core.sessions import SessionStore, validate_session_ref

MAX_RECORD_BYTES = 4 * 1024 * 1024
MAX_RECORD_FILES = 10_000
MAX_JSON_NESTING = 128


class BoundedRecordReader(Protocol):
    remaining: int

    def read(self, relative: Path, limit: int = MAX_RECORD_BYTES) -> bytes: ...

    def names(self, relative: Path, *, limit: int) -> tuple[list[str], bool]: ...


class RecordError(ValueError):
    pass


class ReadBudgetExceeded(RecordError):
    """A source allowance was exhausted before its bytes could be verified."""


class UnsafeRecordError(RecordError):
    """An observed path/snapshot was refused, distinct from absent legacy data."""


class SessionDirectoryBinding:
    """An in-process directory grant valid only during its issuing reader scope."""

    def __init__(self, reader, session_id, held):
        self._reader, self._session_id, self._held = reader, session_id, held

    def check(self):
        if self._reader._selected_fds.get(self._session_id) is not self._held:
            raise UnsafeRecordError("selected session directory scope has ended")
        self._reader._check_selection(self._session_id, *self._held)

    def open_directory(self, relative: Path) -> int:
        self.check()
        parts = () if relative == Path() else _parts(relative)
        return self._reader._record_directory((self._session_id, *parts))


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
    if isinstance(store, BoundedSessionRecords):
        return store._record_directory(parts)
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


def read_bytes(store, relative, *, limit: int | None = MAX_RECORD_BYTES):
    parts = _parts(relative)
    directory = None
    try:
        directory = _directory(store, parts[:-1])
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NONBLOCK | getattr(os, 'O_NOFOLLOW', 0), dir_fd=directory)
        try:
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode):
                raise UnsafeRecordError('record is not a regular file')
            # The authorized replay owner may request the entire saved file.
            # Its observed regular-file size still bounds this snapshot and any
            # concurrent growth; projections continue to pass their fixed limit.
            snapshot_limit = before.st_size if limit is None else limit
            data = bytearray()
            while block := os.read(fd, 65536):
                data.extend(block)
                if len(data) > snapshot_limit:
                    raise ReadBudgetExceeded('record exceeds its read budget')
            after = os.fstat(fd)
            if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                raise UnsafeRecordError('record changed while being read')
            return bytes(data)
        finally:
            os.close(fd)
    except OSError as exc:
        error = RecordError if isinstance(exc, FileNotFoundError) else UnsafeRecordError
        raise error('record is missing, unreadable or symlinked') from exc
    finally:
        if directory is not None:
            os.close(directory)


def parse_json_value(data):
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
    pending = [(result, 0)]
    while pending:
        value, depth = pending.pop()
        if isinstance(value, (dict, list)):
            if depth > MAX_JSON_NESTING:
                raise RecordError('record exceeds its JSON nesting budget')
            children = value.values() if isinstance(value, dict) else value
            pending.extend((child, depth + 1) for child in children)
    return result


def parse_json(data):
    result = parse_json_value(data)
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
        self._store_binding = None
        self._session_bindings = {}
        self._selected_fds = {}

    @staticmethod
    def _identity(info):
        return info.st_dev, info.st_ino

    def _open_bound_root(self):
        fd = os.open(self.root.resolve(), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        identity = self._identity(os.fstat(fd))
        if self._store_binding is not None and identity != self._store_binding:
            os.close(fd)
            raise UnsafeRecordError("selected session store binding changed")
        self._store_binding = identity
        return fd

    def _check_selection(self, session_id, root_fd, session_fd):
        try:
            root = os.stat(self.root)
            selected = os.stat(session_id, dir_fd=root_fd, follow_symlinks=False)
            if (not stat.S_ISDIR(root.st_mode) or not stat.S_ISDIR(selected.st_mode)
                    or self._identity(root) != self._identity(os.fstat(root_fd))
                    or self._identity(selected) != self._identity(os.fstat(session_fd))):
                raise UnsafeRecordError("selected session directory binding changed")
        except OSError as exc:
            raise UnsafeRecordError("selected session directory binding is unavailable") from exc

    @contextmanager
    def selected_session(self, session_id):
        """Hold the authorized directory identities across discovery and reads."""
        parts = _parts(session_id)
        if len(parts) != 1:
            raise RecordError("selected record directory must be one store-relative component")
        session_id = parts[0]
        held = self._selected_fds.get(session_id)
        if held is not None:
            self._check_selection(session_id, *held)
            yield SessionDirectoryBinding(self, session_id, held)
            self._check_selection(session_id, *held)
            return
        root_fd = session_fd = None
        try:
            try:
                root_fd = self._open_bound_root()
                session_fd = os.open(session_id, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                     dir_fd=root_fd)
            except OSError as exc:
                error = RecordError if isinstance(exc, FileNotFoundError) else UnsafeRecordError
                raise error("selected session directory is missing, unreadable or symlinked") from exc
            identity = self._identity(os.fstat(session_fd))
            if (session_id in self._session_bindings
                    and identity != self._session_bindings[session_id]):
                raise UnsafeRecordError("selected session directory binding changed")
            self._session_bindings[session_id] = identity
            self._check_selection(session_id, root_fd, session_fd)
            held = (root_fd, session_fd)
            self._selected_fds[session_id] = held
            try:
                yield SessionDirectoryBinding(self, session_id, held)
                self._check_selection(session_id, root_fd, session_fd)
            finally:
                del self._selected_fds[session_id]
        finally:
            if session_fd is not None:
                os.close(session_fd)
            if root_fd is not None:
                os.close(root_fd)

    def _record_directory(self, parts):
        if not parts:
            return self._open_bound_root()
        directory = None
        try:
            with self.selected_session(parts[0]):
                directory = os.dup(self._selected_fds[parts[0]][1])
                for part in parts[1:]:
                    child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                    dir_fd=directory)
                    os.close(directory)
                    directory = child
            return directory
        except BaseException:
            if directory is not None:
                os.close(directory)
            raise

    def read(self, relative: Path, limit: int = MAX_RECORD_BYTES) -> bytes:
        # Oversize detection can consume one extra 64 KiB chunk. Reserve it
        # before reading, including failures, to keep the aggregate bound real.
        allowance = min(limit, self.remaining - 65536)
        if allowance <= 0:
            raise ReadBudgetExceeded("aggregate source read budget exhausted")
        reserved = allowance + 65536
        self.remaining -= reserved
        with self.selected_session(_parts(relative)[0]):
            data = read_bytes(self, relative, limit=allowance)
        self.remaining += reserved - len(data)
        return data

    def names(self, relative: Path, *, limit: int) -> tuple[list[str], bool]:
        """Bounded enumeration anchored through the same no-follow store root."""
        if type(limit) is not int or not 1 <= limit <= MAX_RECORD_FILES:
            raise RecordError("record directory entry limit is outside its bounded range")
        directory = None
        try:
            directory = _directory(self, _parts(relative))
            names = []
            with os.scandir(directory) as entries:
                for index, entry in enumerate(entries):
                    if index >= limit:
                        return sorted(names), True
                    names.append(entry.name)
            return sorted(names), False
        except OSError as exc:
            error = RecordError if isinstance(exc, FileNotFoundError) else UnsafeRecordError
            raise error("selected record directory is missing, unreadable or symlinked") from exc
        finally:
            if directory is not None:
                os.close(directory)

    def load_meta(self, session_id):
        session_id = validate_session_ref(session_id)
        if session_id not in self._records:
            self._records[session_id] = parse_json(self.read(Path(session_id) / "meta.json"))
        with self.selected_session(session_id):
            return self._records[session_id]

    def load_messages(self, session_id):
        """Replay a complete checkpoint through the retained source directory."""
        from garuda.core.sessions import message_from_dict

        session_id = validate_session_ref(session_id)
        values = parse_json_value(read_bytes(self, Path(session_id) / "messages.json", limit=None))
        if not isinstance(values, list):
            raise RecordError("saved messages must be a JSON list")
        return [message_from_dict(value) for value in values]

    def load_event_trail(self, session_id):
        """Recovery owns a full trail snapshot, independently of projection budgets."""
        session_id = validate_session_ref(session_id)
        try:
            return read_bytes(self, Path(session_id) / "events.jsonl", limit=None).decode("utf-8")
        except RecordError as exc:
            if not isinstance(exc, UnsafeRecordError) and isinstance(exc.__cause__, FileNotFoundError):
                return None
            raise

    def load_state(self, session_id):
        """Authorized native replay, separate from bounded artifact discovery."""
        session_id = validate_session_ref(session_id)
        try:
            return parse_json(read_bytes(self, Path(session_id) / "state.json", limit=MAX_RECORD_BYTES))
        except UnsafeRecordError:
            raise
        except RecordError:
            return {}

    def resolve_for_resume(self, reference, *, workspace=None, all_projects=False):
        """Runtime selection through the existing project/name authority owners.

        Unlike read-only inspection's resolve_ref, this is an authorized native
        launch operation. Existing SessionStore project identity/name semantics
        remain the owner; every selected metadata read retains directory identity.
        """
        root_fd = self._open_bound_root()
        try:
            session_id = super().resolve(reference, workspace=workspace, all_projects=all_projects)
            if self.remaining <= 65536:
                raise ReadBudgetExceeded("resume selection metadata budget exhausted")
            with self.selected_session(session_id):
                meta = self.load_meta(session_id)
                if meta.get("session_id") != session_id:
                    raise RecordError("selected session identity conflicts with its directory")
                # Full ids retain their explicit cross-project meaning. Names,
                # prefixes and latest must still belong to the selected project.
                if workspace is not None and not all_projects and reference != session_id:
                    from garuda.core.project_identity import project_identity

                    identity = project_identity(self.root, workspace)
                    if not self._in_project(meta, identity.project_id, identity.path):
                        raise RecordError("selected session does not belong to the authorized project")
            return session_id
        finally:
            os.close(root_fd)

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
