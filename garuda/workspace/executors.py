"""Durable records of workspace executors that run outside the owner's process tree (#730).

A tmux session and a docker or remote container run the agent's commands in
processes that do not descend from the Garuda process holding the workspace
lease: a tmux pane belongs to the tmux server, and a container to the Docker
daemon. Neither carries the owner's lineage token, so the dead-owner lineage
scan (`garuda.runtime.ownership`) cannot see them, and they keep writing to the
workspace after the owner is killed.

So each such executor is recorded next to the workspace leases *before* it
starts, with what is needed to find it again later (tmux socket and session
name; docker endpoint and container name), and the record is removed only once
a clean stop succeeded. ``LeaseStore.reclaim_dead`` refuses while any record
for the workspace remains whose executor is not proved gone; a record whose
executor is proved gone is removed then. Proof is a positive "no such session
/ container" answer from the recorded server; anything else (the tool is
missing, the server cannot be reached, an unknown answer) is not proof.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import subprocess
import time
from pathlib import Path

from garuda.workspace.lease import default_leases_root, workspace_key

#: Bound on each proof probe, so a hung server cannot hold the lease lock.
PROBE_TIMEOUT_SEC = 10.0

_TMUX_NO_SERVER = ("no server running", "error connecting to")
_DOCKER_GONE = re.compile(r"no such (object|container)", re.IGNORECASE)


class ExecutorRecordError(RuntimeError):
    """An executor record could not be written; the executor must not start."""


def executors_root(leases_root: str | Path | None = None) -> Path:
    return Path(leases_root) if leases_root else default_leases_root()


def _workspace_dir(root: Path, workspace: str | Path) -> Path:
    digest = hashlib.sha256(workspace_key(workspace).encode()).hexdigest()[:32]
    return root / "executors" / digest


def _record_path(root: Path, workspace: str | Path, kind: str, name: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", name)[:120]
    return _workspace_dir(root, workspace) / f"{kind}-{safe}.json"


def record_executor(workspace: str | Path, kind: str, name: str, *,
                    leases_root: str | Path | None = None, **locator) -> Path:
    """Durably record an out-of-tree executor before it starts. Raises on failure."""
    from garuda.runtime.ownership import current_owner

    root = executors_root(leases_root)
    path = _record_path(root, workspace, kind, name)
    owner = current_owner()
    record = {"version": 1, "workspace": workspace_key(workspace), "kind": kind, "name": name,
              **locator, "owner": {"pid": owner.pid, "identity": owner.identity},
              "recorded_at": time.time()}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(record, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except OSError as exc:
        raise ExecutorRecordError(
            f"cannot record the {kind} executor {name!r} for {workspace_key(workspace)}: {exc}"
        ) from exc
    return path


def update_executor(path: Path, **locator) -> None:
    """Merge newly learned locator fields (e.g. the real tmux socket) into a record."""
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
        record.update(locator)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(record), encoding="utf-8")
        os.replace(tmp, path)
    except (OSError, ValueError):
        pass  # the record written before start still stands


def forget_executor(path: Path | None) -> None:
    """Remove a record after its executor was stopped cleanly."""
    if path is None:
        return
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def executor_records(workspace: str | Path, *, leases_root: str | Path | None = None
                     ) -> list[tuple[Path, dict | None]]:
    """Every executor record for ``workspace``; an unreadable one is ``(path, None)``."""
    directory = _workspace_dir(executors_root(leases_root), workspace)
    try:
        paths = sorted(p for p in directory.iterdir()
                       if p.suffix == ".json" and not p.name.startswith("."))
    except FileNotFoundError:
        return []
    records = []
    for path in paths:
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            records.append((path, record if isinstance(record, dict) else None))
        except (OSError, ValueError):
            records.append((path, None))
    return records


def default_tmux_socket() -> str:
    """The socket a plain ``tmux`` client of this process talks to."""
    inside = os.environ.get("TMUX", "")
    if inside:
        return inside.split(",", 1)[0]
    base = os.environ.get("TMUX_TMPDIR") or "/tmp"
    return os.path.join(base, f"tmux-{os.getuid()}", "default")


def docker_locator(host: str | None) -> dict:
    """Which daemon a ``docker`` client of this process talks to, for later proof."""
    return {"docker_host": host or os.environ.get("DOCKER_HOST") or None,
            "docker_context": os.environ.get("DOCKER_CONTEXT") or None}


def _probe(argv: list[str]) -> tuple[int | None, str]:
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=PROBE_TIMEOUT_SEC,
                              stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as exc:
        return None, str(exc)
    return done.returncode, f"{done.stdout}\n{done.stderr}".strip()


def _earlier_boot(record: dict) -> bool:
    """Whether the recording owner ran in an earlier boot of this Linux host."""
    identity = (record.get("owner") or {}).get("identity")
    if not isinstance(identity, str) or not identity.startswith("linux:") or identity.count(":") != 2:
        return False
    boot = identity.split(":")[1]
    try:
        current = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        return False
    return bool(boot and current and boot != current)


def _tmux_servers() -> list[int] | None:
    """Pids of this user's tmux server processes; None when they cannot be listed."""
    if not os.path.isdir("/proc/self"):
        return None
    uid = os.getuid()
    found = []
    try:
        entries = os.listdir("/proc")
    except OSError:
        return None
    for entry in entries:
        if not entry.isdigit():
            continue
        try:
            if os.stat(f"/proc/{entry}").st_uid != uid:
                continue
            with open(f"/proc/{entry}/cmdline", "rb") as handle:
                argv0 = handle.read().split(b"\0", 1)[0]
            with open(f"/proc/{entry}/comm", "rb") as handle:
                comm = handle.read().strip()
        except (FileNotFoundError, ProcessLookupError):
            continue
        except OSError:
            return None
        if argv0.startswith(b"tmux: server") or (comm == b"tmux: server"):
            found.append(int(entry))
    return found


def executor_gone(record: dict) -> tuple[bool, str]:
    """``(True, why)`` only when the recorded executor is proved gone."""
    kind, name = record.get("kind"), record.get("name")
    if not isinstance(name, str) or not name:
        return False, "the record names no executor"
    if kind == "tmux":
        socket = record.get("socket")
        if not isinstance(socket, str) or not socket:
            return False, "the record names no tmux socket"
        if _earlier_boot(record):
            return True, "it was started before this machine last booted"
        code, out = _probe(["tmux", "-S", socket, "has-session", "-t", f"={name}"])
        if code == 0:
            return False, "the tmux session is still running"
        if code is not None and "can't find session" in out.lower():
            return True, "its tmux server reports no such session"
        if code is not None and any(mark in out.lower() for mark in _TMUX_NO_SERVER):
            # Nothing answers on the socket. That proves the session gone only
            # when no tmux server of this user runs at all: a server whose
            # socket file was removed still runs its panes.
            servers = _tmux_servers()
            if servers == []:
                return True, "no tmux server is running"
            if servers:
                return False, (f"its socket does not answer, but tmux server process(es) "
                               f"{', '.join(map(str, servers))} still run")
        return False, f"tmux could not confirm the session is gone ({out[:200] or code})"
    if kind in ("docker", "remote"):
        argv = ["docker"]
        host = record.get("docker_host")
        if host:
            argv += ["-H", host]
        context = record.get("docker_context")
        if context and not host:
            argv += ["--context", context]
        code, out = _probe([*argv, "inspect", "--type", "container", "--format",
                            "{{.State.Status}}", name])
        if code == 0:
            return False, f"the container still exists (state {out.strip()[:40] or 'unknown'})"
        if code is not None and _DOCKER_GONE.search(out):
            return True, "docker reports no such container"
        return False, f"docker could not confirm the container is gone ({out[:200] or code})"
    return False, f"unknown executor kind {kind!r}"


def stop_hint(record: dict | None, path: Path) -> str:
    if not isinstance(record, dict):
        return f"inspect and remove the unreadable executor record {path}"
    name = str(record.get("name"))
    if record.get("kind") == "tmux":
        socket = record.get("socket") or default_tmux_socket()
        return f"stop it with `tmux -S {shlex.quote(str(socket))} kill-session -t {shlex.quote('=' + name)}`"
    if record.get("kind") in ("docker", "remote"):
        host = record.get("docker_host")
        prefix = f"docker -H {shlex.quote(host)}" if host else "docker"
        return f"stop it with `{prefix} rm -f {shlex.quote(name)}`"
    return f"stop it, then remove the executor record {path}"


def executor_refusal(workspace: str | Path, *, leases_root: str | Path | None = None) -> str | None:
    """Why ``workspace`` may still be written by a recorded executor, or None.

    Records whose executor is proved gone are removed on the way.
    """
    for path, record in executor_records(workspace, leases_root=leases_root):
        if record is None:
            return f"its executor record {path} is unreadable; {stop_hint(None, path)}"
        gone, why = executor_gone(record)
        if gone:
            forget_executor(path)
            continue
        return (f"its {record.get('kind')} executor {record.get('name')!r} may still be writing "
                f"to the workspace ({why}); {stop_hint(record, path)}, then retry")
    return None
