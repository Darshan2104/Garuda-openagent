"""Fixed acceptance probe/publication operations, invoked only by an owned worker."""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import stat
import sys
import time
from pathlib import Path

from garuda.core.acceptance_records import CheckRecords
from garuda.runtime.private_records import FLAGS, open_directory, private_file, read_record


class AuthorityChanged(Exception):
    pass


class OwnershipChanged(Exception):
    pass


class WorkspaceChanged(Exception):
    pass


def _owner(data):
    from garuda.runtime.recovery import _process_identity

    snapshot = data["snapshot"]
    if not isinstance(snapshot, dict) or set(snapshot) != {"root", "lease"}:
        raise ValueError("invalid retained ownership snapshot")
    lease = snapshot["lease"]
    keys = {"workspace", "session_id", "mode", "pid", "identity", "pgid", "epoch"}
    if not isinstance(lease, dict) or set(lease) != keys or lease["mode"] != "mutating":
        raise ValueError("invalid mutating ownership binding")
    if os.path.realpath(lease["workspace"]) != lease["workspace"]:
        raise ValueError("workspace binding changed")
    directory = open_directory(Path(snapshot["root"]))
    lock = None
    try:
        info = os.fstat(directory)
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise ValueError("lease directory is not private")
        lock = os.open(".lock", os.O_RDWR | FLAGS, dir_fd=directory)
        private_file(lock)
        deadline = time.monotonic() + 5
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise ValueError("lease read lock is unavailable") from None
                time.sleep(0.01)
        name = hashlib.sha256(lease["workspace"].encode()).hexdigest()[:32] + ".json"
        record = read_record(directory, name, versions=(2,))
        if record is None or set(record) != {"version", "holders"} or not isinstance(record["holders"], list):
            raise ValueError("durable lease record is unavailable")
        holders = [holder for holder in record["holders"] if holder.get("session_id") == lease["session_id"]]
        if (len(holders) != 1 or any(holders[0].get(key) != lease[key] for key in keys)
                or _process_identity(lease["pid"]) != lease["identity"]):
            raise ValueError("retained issuer no longer matches durable ownership")
        holder = holders[0]
        if (len(record["holders"]) != 1
                or any(type(holder.get(key)) not in (int, float) or not math.isfinite(holder[key])
                       for key in ("acquired_at", "heartbeat_at", "ttl_sec"))
                or holder["ttl_sec"] <= 0):
            raise ValueError("durable mutating lease has invalid liveness evidence")
        if time.time() - holder["heartbeat_at"] > holder["ttl_sec"]:
            raise ValueError("parent heartbeat expired")
        return {"valid": True}
    finally:
        if lock is not None:
            os.close(lock)
        os.close(directory)


def _sources(data):
    sources = data["sources"]
    if not isinstance(sources, list) or len(sources) > 32:
        raise ValueError("invalid frozen source list")
    for source in sources:
        if not isinstance(source, dict) or set(source) != {"path", "digest"}:
            raise ValueError("invalid frozen source")
        path = Path(source["path"])
        parent = open_directory(path.parent)
        fd = None
        try:
            fd = os.open(path.name, os.O_RDONLY | FLAGS, dir_fd=parent)
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ValueError("source is not a regular file")
            with os.fdopen(os.dup(fd), "rb") as handle:
                raw = handle.read(16 * 1024 * 1024 + 1)
            if len(raw) > 16 * 1024 * 1024 or hashlib.sha256(raw).hexdigest() != source["digest"]:
                raise ValueError("frozen source differs")
        finally:
            if fd is not None:
                os.close(fd)
            os.close(parent)
    return {"valid": True}


def _fingerprint(data):
    # This process is disposable and has no unrelated runtime work. Preserve the
    # existing fingerprint algorithm while disabling repository Git helpers and
    # inherited redirection/index state for these owned read-only probes.
    for key in tuple(os.environ):
        if key.startswith("GIT_"):
            del os.environ[key]
    config = {"core.fsmonitor": "false", "core.hooksPath": os.devnull,
              "core.untrackedCache": "false"}
    os.environ.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
                      GIT_CONFIG_COUNT=str(len(config)), GIT_TERMINAL_PROMPT="0")
    for index, (key, value) in enumerate(config.items()):
        os.environ[f"GIT_CONFIG_KEY_{index}"] = key
        os.environ[f"GIT_CONFIG_VALUE_{index}"] = value
    from garuda.core.acceptance import fingerprint

    value = fingerprint(data["workspace"])
    if value is None:
        raise ValueError("candidate fingerprint is unavailable")
    return {"fingerprint": value}


def _context(data):
    try:
        _owner({"snapshot": data["snapshot"]})
    except Exception as exc:
        raise OwnershipChanged from exc
    workspace = data["workspace"]
    if data["snapshot"]["lease"]["workspace"] != workspace or os.path.realpath(workspace) != workspace:
        raise WorkspaceChanged("workspace scope changed")
    info = os.stat(workspace, follow_symlinks=False)
    if (not stat.S_ISDIR(info.st_mode) or [info.st_dev, info.st_ino] != data["workspace_identity"]):
        raise WorkspaceChanged("workspace directory identity changed")
    cwd = os.path.realpath(os.path.join(workspace, data["cwd"]))
    if cwd != workspace and Path(workspace) not in Path(cwd).parents:
        raise WorkspaceChanged("check cwd leaves workspace")
    if not os.path.isdir(cwd):
        raise ValueError("check cwd is unavailable")
    try:
        _sources({"sources": data["sources"]})
    except Exception as exc:
        raise AuthorityChanged from exc
    return {**_fingerprint({"workspace": workspace}), "cwd": cwd}


def dispatch(operation, data):
    if not isinstance(data, dict):
        raise ValueError("operation input is not a mapping")
    schemas = {"admitted-sources": {"sources"}, "fingerprint": {"workspace"}, "sources": {"sources"}, "owner": {"snapshot"},
               "context": {"snapshot", "workspace", "workspace_identity", "cwd", "sources"},
               "prepare": {"session_dir", "binding", "summary", "limits"},
               "transition": {"session_dir", "binding", "expected", "phase", "fields"},
               "terminal": {"session_dir", "binding", "receipt"},
               "reconcile": {"session_dir", "binding"}}
    if operation not in schemas or set(data) != schemas[operation]:
        raise ValueError("unsupported operation or fields")
    if operation == "admitted-sources":
        from garuda.config_sources import verify_sources

        return verify_sources(data["sources"])
    if operation == "fingerprint":
        return _fingerprint(data)
    if operation == "sources":
        return _sources(data)
    if operation == "owner":
        return _owner(data)
    if operation == "context":
        return _context(data)
    store = CheckRecords(data["session_dir"], data["binding"])
    if operation == "prepare":
        return store.prepare(data["summary"], data["limits"])
    if operation == "transition":
        return store.transition(data["expected"], data["phase"], **data["fields"])
    if operation == "terminal":
        return store.terminal(data["receipt"])
    return store.reconcile()


def main():
    try:
        fd = int(os.environ.pop("GARUDA_OWNED_INPUT_FD"))
        with os.fdopen(fd, "rb") as handle:
            raw = handle.read(65_537)
        if len(raw) > 65_536:
            raise ValueError("owned input exceeds its bound")
        request = json.loads(raw)
        if not isinstance(request, dict) or set(request) != {"operation", "data"}:
            raise ValueError("invalid owned operation envelope")
        value = dispatch(request["operation"], request["data"])
        response = {"ok": True, "value": value}
    except Exception as exc:
        # Source paths, commands, raw environment, and incidental exception
        # text are not an operation's diagnostic channel.
        response = {"ok": False, "value": type(exc).__name__}
    raw = json.dumps(response, sort_keys=True)
    if len(raw) > 3000:
        raw = json.dumps({"ok": False, "value": "OperationResultTooLarge"})
    sys.stdout.write(raw)


if __name__ == "__main__":
    main()
