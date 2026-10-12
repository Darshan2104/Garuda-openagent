"""Private single-command Linux subreaper; stdout is a bounded control channel."""

from __future__ import annotations

import ctypes
import json
import os
import selectors
import signal
import subprocess
import sys
import time
from pathlib import Path

from garuda.runtime.process_supervision import TAIL_BYTES, WIRE_LIMIT, ProcessRequest
from garuda.runtime.recovery import _process_identity


def _emit(launch_id: str, kind: str, **fields) -> None:
    print(json.dumps({"version": 1, "launch_id": launch_id, "kind": kind, **fields}), flush=True)


def _children() -> list[int]:
    # All child creation occurs in this worker's main thread.
    return [int(v) for v in Path(f"/proc/self/task/{os.getpid()}/children").read_text().split()]


def _signal_children(sig: int) -> None:
    for pid in _children():
        try:
            fd = os.pidfd_open(pid)
        except ProcessLookupError:
            continue
        try:
            # Revalidate parentage after opening the stable signal handle.
            # Reuse outside this subtree cannot authorize a signal.
            if pid in _children():
                signal.pidfd_send_signal(fd, sig)
        except ProcessLookupError:
            pass
        finally:
            os.close(fd)


def _reap(proc) -> bool:
    while True:
        try:
            pid, status = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return True  # ECHILD from the established subreaper.
        if pid == 0:
            return False
        if proc is not None and pid == proc.pid:
            proc.returncode = os.waitstatus_to_exitcode(status)


def _close_tree(term: float, kill: float, drain, proc) -> bool:
    for sig, grace in ((signal.SIGTERM, term), (signal.SIGKILL, kill)):
        deadline = time.monotonic() + grace
        while True:
            if _reap(proc):
                return True
            _signal_children(sig)
            drain(0.01)
            if time.monotonic() >= deadline:
                break
    return _reap(proc)


def main() -> int:
    launch_id = sys.argv[1]
    if str(__import__("uuid").UUID(launch_id)) != launch_id:
        return 2
    libc = ctypes.CDLL(None, use_errno=True)
    parent = os.getppid()
    cancelled = False

    def cancel(*_):
        nonlocal cancelled
        cancelled = True

    signal.signal(signal.SIGTERM, cancel)
    # Never use SIGCHLD=SIG_IGN: ECHILD must come from actually reaped children.
    signal.signal(signal.SIGCHLD, signal.SIG_DFL)
    if libc.prctl(36, 1, 0, 0, 0) != 0 or libc.prctl(1, signal.SIGTERM, 0, 0, 0) != 0:
        return 2
    if os.getppid() != parent:
        return 2
    owner = {"pid": os.getpid(), "identity": _process_identity(os.getpid()),
             "pgid": os.getpgrp()}
    if not owner["identity"]:
        return 2
    _emit(launch_id, "ready", supervisor=owner)
    raw = sys.stdin.buffer.readline(WIRE_LIMIT + 1)
    command = None
    started = time.monotonic()
    tails = {"stdout_tail": bytearray(), "stderr_tail": bytearray()}
    selector = selectors.DefaultSelector()

    def drain(timeout):
        nonlocal cancelled
        for key, _ in selector.select(timeout):
            data = os.read(key.fd, 16_384)
            if key.data == "control":
                cancelled = True
                selector.unregister(key.fd)
            elif not data:
                selector.unregister(key.fd)
            else:
                tails[key.data].extend(data)
                del tails[key.data][:-TAIL_BYTES]

    proc = None
    outcome, exit_code = "cancelled", None
    request = ProcessRequest("true", "/")
    try:
        if raw and not cancelled:
            if len(raw) > WIRE_LIMIT or not raw.endswith(b"\n"):
                raise ValueError("incomplete or oversized request")
            data = json.loads(raw)
            if not isinstance(data, dict) or set(data) != {
                    "version", "run", "cwd", "env", "timeout", "term_grace", "kill_grace", "input_data"}:
                raise ValueError("invalid request")
            if type(data["version"]) is not int or data["version"] != 1:
                raise ValueError("unsupported request")
            run = tuple(data["run"]) if isinstance(data["run"], list) else data["run"]
            request = ProcessRequest(run, data["cwd"], tuple(data["env"].items()),
                                     data["timeout"], data["term_grace"], data["kill_grace"], data["input_data"])
            request.document()
            env = {**os.environ, **dict(request.env)}
            input_fd = None
            try:
                if request.input_data is not None:
                    # Trusted helper payloads stay in an anonymous descriptor,
                    # never a command-line argument or a raw-output file.
                    input_fd = os.memfd_create("garuda-owned-input", os.MFD_CLOEXEC)
                    with os.fdopen(os.dup(input_fd), "wb") as handle:
                        handle.write(request.input_data.encode())
                    os.lseek(input_fd, 0, os.SEEK_SET)
                    env["GARUDA_OWNED_INPUT_FD"] = str(input_fd)
                proc = subprocess.Popen(
                    run, shell=isinstance(run, str), cwd=request.cwd, env=env,
                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    pass_fds=() if input_fd is None else (input_fd,), start_new_session=True)
            finally:
                if input_fd is not None:
                    os.close(input_fd)
            command = {"pid": proc.pid, "identity": _process_identity(proc.pid), "pgid": proc.pid}
            if not command["identity"]:
                raise RuntimeError("unknown command identity")
            _emit(launch_id, "started", command=command)
            selector.register(sys.stdin.fileno(), selectors.EVENT_READ, "control")
            for stream, name in ((proc.stdout, "stdout_tail"), (proc.stderr, "stderr_tail")):
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ, name)
            outcome = "exited"
            while True:
                exit_code = proc.poll()
                if cancelled:
                    outcome = "cancelled"
                    break
                if time.monotonic() - started >= request.timeout:
                    outcome = "timeout"
                    break
                if exit_code is not None:
                    break
                drain(0.01)
    except (OSError, ValueError, TypeError, RuntimeError):
        outcome = "launch-failed"
        if command is None:
            # A failed launch has no started message; its terminal evidence is
            # intentionally insufficient for a parent expecting activation.
            pass
    finally:
        # One shared TERM/KILL deadline includes leader and adopted descendants.
        closed = _close_tree(request.term_grace, request.kill_grace, drain, proc)
        if proc is not None:
            exit_code = proc.returncode
        deadline = time.monotonic() + 0.2
        while (any(key.data != "control" for key in selector.get_map().values())
               and time.monotonic() < deadline):
            drain(0.01)
        selector.close()
    if not closed:
        return 3
    _emit(launch_id, "terminal", supervisor=owner, command=command,
          outcome=outcome, exit_code=exit_code,
          cleanup={"backend": "linux-subreaper", "echild": True},
          duration_s=round(time.monotonic() - started, 6),
          **{key: value.decode(errors="replace")[-TAIL_BYTES:] for key, value in tails.items()})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
