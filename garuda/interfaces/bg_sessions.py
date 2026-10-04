"""Background sessions: ``garuda run --bg`` and its detached worker (plan task D.2, #167).

``run --bg`` does four things and returns: it creates the session (state
``queued``), writes the launch description beside it, adds an entry to the durable
queue (D.1), and starts a hidden worker as a detached process. Then it prints the
session id. Nothing else about the run happens in the caller.

The **worker** (``garuda __worker SESSION``):

1. records its pid, process start identity and command identity — before it
   claims anything or can change the workspace;
2. waits for its turn in the queue **without holding a workspace lease** (so a
   waiting worker never blocks the workspace it will use), polling with bounded
   backoff, and noticing a queued cancellation;
3. claims capacity (the queue takes the slot from the shared capacity store),
   marks the session working, and runs the ordinary ``garuda run`` path with the
   preassigned session id;
4. on every way out — completion, failure, cancellation, an exception — releases
   its queue claim (and with it the capacity slot), stops its heartbeat and leaves
   the session in a terminal state. A worker killed outright cannot do that;
   its claim is reclaimed on proof of death and the session reads ``crashed``
   (derived, never stored).

**Cancellation.** A queued session is removed from the queue and marked cancelled.
A running one is sent SIGTERM through its process group — but only after its
recorded process identity is checked against the live process, so a recycled pid
is never signalled.

**Logs** are bounded: the worker writes ``worker.log`` in the session directory
and stops at a fixed size, noting the cut.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import json
import os
import signal
import subprocess
import sys
import traceback
from pathlib import Path

LAUNCH_FILE = "launch.json"
LOG_FILE = "worker.log"
LOG_LIMIT = 1_000_000
POLL_MAX = 2.0
HEARTBEAT_EVERY = 10.0
WORKER_COMMAND = "__worker"


class BackgroundRefused(Exception):
    """A background request that cannot be honoured; nothing was started."""


class BoundedLog:
    """A text stream that keeps the first ``limit`` bytes and says so once."""

    def __init__(self, path: Path, limit: int = LOG_LIMIT):
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
        self._fd = os.open(path, flags, 0o600)
        self._limit = limit
        self._written = os.fstat(self._fd).st_size
        self._cut = False

    def write(self, text: str) -> int:
        if not text:
            return 0
        data = text.encode("utf-8", errors="replace")
        room = self._limit - self._written
        if room <= 0:
            self._mark_cut()
            return len(text)
        os.write(self._fd, data[:room])
        self._written += min(len(data), room)
        if len(data) > room:
            self._mark_cut()
        return len(text)

    def _mark_cut(self) -> None:
        if not self._cut:
            self._cut = True
            os.write(self._fd, f"\n[log cut at {self._limit} bytes]\n".encode())

    def flush(self) -> None:
        return None

    def isatty(self) -> bool:
        return False

    def close(self) -> None:
        with contextlib.suppress(OSError):
            os.close(self._fd)


# --- launching ---------------------------------------------------------------------------


def _jsonable(args: argparse.Namespace) -> dict:
    out = {}
    for key, value in vars(args).items():
        if key.startswith("_") or key in ("bg",):
            continue
        try:
            json.dumps(value)
        except (TypeError, ValueError):
            continue
        out[key] = value
    return out


def _digest(data: dict) -> str:
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode("utf-8")).hexdigest()


def _write_private(path: Path, data: dict) -> None:
    from garuda.runtime.strict_store import write_document

    write_document(path, data)


def harness_of(args) -> str:
    return getattr(args, "runtime", None) or "native"


def scope_of(args) -> str:
    from garuda.runtime.queue import scope_for

    return scope_for(harness_of(args))


def launch(args: argparse.Namespace, *, store=None, queue=None, spawn=None) -> str:
    """Create the session and its queue entry, start the worker, return the session id."""
    import uuid

    from garuda.core.sessions import SessionStore
    from garuda.runtime import session_state
    from garuda.runtime.queue import QueueStore

    task = args.task
    if getattr(args, "file", None):
        task = Path(args.file).read_text(encoding="utf-8")
    if not task:
        raise BackgroundRefused("provide -t/--task or -f/--file")
    for flag in ("resume", "trajectory"):
        if getattr(args, flag, None):
            raise BackgroundRefused(f"--bg does not combine with --{flag.replace('_', '-')}")
    store = store or SessionStore()
    queue = queue or QueueStore()
    description = _jsonable(args)
    description.update({"task": task, "file": None, "json": False})
    session_id = str(uuid.uuid4())
    store.begin(session_id, task=task, model=str(getattr(args, "model", None) or ""),
                agent=str(getattr(args, "agent", "build")),
                workspace=os.path.realpath(args.workspace),
                name=getattr(args, "name", None))
    directory = store.session_dir(session_id)
    digest = _digest(description)
    _write_private(directory / LAUNCH_FILE, {"version": 1, "args": description,
                                             "config_digest": digest})
    store.update_meta(session_id, {"status": "queued", "state": session_state.queued(),
                                   "background": True, "config_digest": digest})
    scope = scope_of(args)
    queue.enqueue(scope, session_id, harness=harness_of(args), session_id=session_id,
                  config_digest=digest)
    try:
        process = (spawn or _spawn_worker)(session_id)
    except BaseException:
        queue.cancel(scope, session_id)
        store.update_meta(session_id, {"status": "failed", "state": session_state.interrupted()})
        raise
    record = worker_record(process)
    store.update_meta(session_id, {"worker": record})
    return session_id


def worker_record(process) -> dict:
    """What identifies the worker process: pid, start identity, group, command."""
    from garuda.runtime.recovery import _process_identity

    try:
        identity = _process_identity(process.pid)
    except Exception:
        identity = None
    command = hashlib.sha256(" ".join(process.args).encode()).hexdigest() \
        if isinstance(getattr(process, "args", None), list) else None
    return {"pid": process.pid, "identity": identity, "pgid": process.pid, "command": command}


def _spawn_worker(session_id: str) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-m", "garuda.interfaces.main", WORKER_COMMAND, session_id],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True, close_fds=True, env=dict(os.environ))


# --- the worker --------------------------------------------------------------------------


def _load_launch(store, session_id: str) -> dict:
    from garuda.runtime.strict_store import read_document

    path = store.session_dir(session_id) / LAUNCH_FILE
    info = os.lstat(path)
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise BackgroundRefused(f"{path} is not owner-only")
    document = read_document(path, versions=(1,))
    if not document or not isinstance(document.get("args"), dict):
        raise BackgroundRefused(f"{path} is not a launch description")
    return document


def _state_of(store, session_id: str) -> dict:
    from garuda.runtime.session_state import effective_state

    return effective_state(store.load_meta(session_id), liveness=lambda owner: True)


def _terminal(store, session_id: str) -> bool:
    from garuda.runtime.session_state import ACTIVE_WORK

    return _state_of(store, session_id).get("work") not in ACTIVE_WORK


async def _heartbeat(queue, scope, session_id, owner, interval):
    while True:
        await asyncio.sleep(interval)
        queue.heartbeat(scope, session_id, owner)


async def run_worker_async(session_id: str, *, store=None, queue=None, runner=None,
                           poll_max: float = POLL_MAX) -> int:
    from garuda.core.sessions import SessionStore
    from garuda.runtime import session_state
    from garuda.runtime.ownership import current_owner
    from garuda.runtime.queue import QueueStore
    from garuda.runtime.recovery import _process_identity

    store = store or SessionStore()
    queue = queue or QueueStore()
    document = _load_launch(store, session_id)
    args = argparse.Namespace(**document["args"])
    args.bg = False
    args._session_id = session_id
    args._queue_claim = True
    scope = scope_of(args)
    owner = current_owner()

    # 1. Identity first: before the queue, a lease, or any change to the workspace.
    try:
        identity = _process_identity(os.getpid())
    except Exception:
        identity = None
    store.update_meta(session_id, {
        "worker": {"pid": os.getpid(), "identity": identity, "pgid": os.getpgid(0),
                   "command": hashlib.sha256(" ".join(sys.argv).encode()).hexdigest()},
        "state": {**session_state.queued(owner.to_dict())}})

    claimed = False
    heartbeat = None
    try:
        # 2. Wait for the turn — holding no workspace lease.
        delay = 0.05
        while True:
            if _terminal(store, session_id):
                return 0  # cancelled (or otherwise ended) while queued
            if queue.try_claim(scope, session_id, owner):
                claimed = True
                break
            if not any(e["id"] == session_id for e in queue.entries(scope)):
                store.update_meta(session_id, {"status": "cancelled",
                                               "state": session_state.interrupted(cancelled=True)})
                return 0
            await asyncio.sleep(delay)
            delay = min(delay * 2, poll_max)
        # Persist launch intent before any runtime can start, including an
        # unlimited harness. Death after this point quarantines the slot.
        queue.begin_dispatch(scope, session_id, owner)
        # 3. Working.
        store.update_meta(session_id, {"status": "running",
                                       "state": session_state.started(owner.to_dict())})
        heartbeat = asyncio.ensure_future(
            _heartbeat(queue, scope, session_id, owner, HEARTBEAT_EVERY))
        if runner is None:
            from garuda.interfaces.main import run_task_guarded as runner
        code = await runner(args)
        if not _terminal(store, session_id):
            store.update_meta(session_id, {
                "status": "success" if code == 0 else "failed",
                "state": session_state.finished(success=code == 0)})
        return code
    except asyncio.CancelledError:
        store.update_meta(session_id, {"status": "cancelled",
                                       "state": session_state.interrupted(cancelled=True)})
        return 130
    except BaseException:
        print(traceback.format_exc(), file=sys.stderr)
        if not _terminal(store, session_id):
            store.update_meta(session_id, {"status": "failed",
                                           "state": session_state.interrupted()})
        return 1
    finally:
        if heartbeat is not None:
            heartbeat.cancel()
        if claimed:
            queue.release(scope, session_id, owner)
        else:
            queue.cancel(scope, session_id)


def run_worker(session_id: str) -> int:
    """Entry point of the hidden ``__worker`` command."""
    from garuda.core.sessions import SessionStore

    store = SessionStore()
    log = BoundedLog(store.session_dir(session_id) / LOG_FILE)
    sys.stdout = sys.stderr = log  # type: ignore[assignment]

    async def main() -> int:
        task = asyncio.ensure_future(run_worker_async(session_id, store=store))
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, task.cancel)
        try:
            return await task
        except asyncio.CancelledError:
            return 130

    try:
        return asyncio.run(main())
    finally:
        log.close()


# --- cancelling --------------------------------------------------------------------------


def cancel(store, session_id: str, *, queue=None, kill=os.killpg) -> str:
    """Cancel a background session; returns what was done."""
    from garuda.runtime import session_state
    from garuda.runtime.queue import QueueStore
    from garuda.runtime.recovery import ProcessIdentityUnavailable, _process_identity, same_process

    queue = queue or QueueStore()
    meta = store.load_meta(session_id)
    if not meta.get("background"):
        raise BackgroundRefused("not a background session")
    state = session_state.effective_state(meta)
    if state.get("work") not in session_state.ACTIVE_WORK:
        return "already finished"
    scope = None
    for entry in queue.entries():
        if entry["id"] == session_id:
            scope = entry["scope"]
            queued = entry["state"] == "queued"
            break
    else:
        queued = False
    if scope is not None and queued and queue.cancel(scope, session_id):
        store.update_meta(session_id, {"status": "cancelled",
                                       "state": session_state.interrupted(cancelled=True)})
        return "removed from the queue before it started"
    worker = meta.get("worker") or {}
    pid, recorded = worker.get("pid"), worker.get("identity")
    if not isinstance(pid, int) or not recorded:
        return "no worker identity is recorded; nothing was signalled"
    try:
        live = _process_identity(pid)
    except ProcessIdentityUnavailable:
        return "the worker's identity cannot be checked; nothing was signalled"
    if live is None:
        store.update_meta(session_id, {"status": "failed", "state": session_state.interrupted()})
        return "the worker is not running"
    if not same_process(recorded, live):
        # The pid now belongs to another process. Never signal it.
        store.update_meta(session_id, {"status": "failed", "state": session_state.interrupted()})
        return "the worker's pid was reused by another process; nothing was signalled"
    kill(worker.get("pgid") or pid, signal.SIGTERM)
    return "asked the running worker to stop"
