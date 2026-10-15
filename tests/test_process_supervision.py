"""Actual Linux child trees, with a negative platform control on other hosts."""

import asyncio
import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from garuda.runtime.process_supervision import (
    TAIL_BYTES,
    CleanupUncertain,
    LinuxProcessSupervisor,
    ProcessRequest,
    SupervisionUnavailable,
)

linux = pytest.mark.skipif(sys.platform != "linux", reason="Linux subreaper conformance")


async def _appears(path: Path):
    deadline = asyncio.get_running_loop().time() + 5
    while not path.exists():
        assert asyncio.get_running_loop().time() < deadline, f"child did not create {path.name}"
        await asyncio.sleep(0.01)


def _signal(fd):
    try:
        signal.pidfd_send_signal(fd, signal.SIGKILL)
    except ProcessLookupError:
        pass
    finally:
        os.close(fd)


@pytest.mark.asyncio
async def test_unsupported_host_refuses_before_creating_a_worker():
    if sys.platform == "linux":
        pytest.skip("negative control belongs to the actual unsupported platform")
    with pytest.raises(SupervisionUnavailable):
        await LinuxProcessSupervisor().prepare()


@linux
@pytest.mark.asyncio
async def test_prepared_supervisor_exits_without_authorizing_a_command():
    async with await LinuxProcessSupervisor().prepare() as owned:
        children = Path(f"/proc/{owned.proc.pid}/task/{owned.proc.pid}/children")
        assert children.read_text().strip() == ""
    assert owned.result.command is None
    assert owned.result.outcome == "cancelled"
    assert owned.result.cleanup["echild"] is True


@linux
@pytest.mark.asyncio
async def test_cancellation_during_real_readiness_still_closes_the_unactivated_worker(monkeypatch):
    from garuda.runtime.process_supervision import OwnedProcess

    entered, release = asyncio.Event(), asyncio.Event()
    original = OwnedProcess._ready
    observed = []

    async def gated_ready(owned):
        observed.append(owned)
        entered.set()
        await release.wait()
        await original(owned)

    monkeypatch.setattr(OwnedProcess, "_ready", gated_ready)
    task = asyncio.create_task(LinuxProcessSupervisor().prepare())
    await entered.wait()
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    owned = observed[0]
    assert owned.result.command is None
    assert owned.result.cleanup["echild"] is True
    assert owned.proc.returncode == 0


@linux
@pytest.mark.asyncio
async def test_unvalidated_readiness_never_authorizes_a_signal(monkeypatch):
    from garuda.runtime.process_supervision import OwnedProcess

    original_message, original_signal = OwnedProcess._message, signal.pidfd_send_signal
    observed, signals = [], []

    async def corrupted_ready(owned):
        message = await original_message(owned)
        if message["kind"] == "ready":
            observed.append(owned)
            message["supervisor"]["identity"] = "unknown"
        return message

    def record_signal(*args):
        signals.append(args)
        return original_signal(*args)

    monkeypatch.setattr(OwnedProcess, "_message", corrupted_ready)
    monkeypatch.setattr(signal, "pidfd_send_signal", record_signal)
    with pytest.raises(CleanupUncertain):
        await LinuxProcessSupervisor().prepare()
    assert signals == []
    assert observed[0].proc.returncode == 0


@linux
@pytest.mark.asyncio
@pytest.mark.parametrize("shell,binary", [(False, False), (True, False), (False, True)],
                         ids=["argv", "shell", "binary"])
async def test_check_drains_both_streams_while_event_loop_progresses(tmp_path, shell, binary):
    import shlex

    stdout = b"\0" if binary else b"o"
    stderr = b"\xff" if binary else b"e"
    script = (
        "import pathlib,sys,time; "
        "assert sys.stdin.read()=='' and sys.argv[-1]==''; "
        f"sys.stdout.buffer.write({stdout!r}*2000000+b'stdout-end');sys.stdout.flush(); "
        f"sys.stderr.buffer.write({stderr!r}*2000000+b'stderr-end');sys.stderr.flush(); "
        "pathlib.Path('entered').touch(); "
        "exec(\"while not pathlib.Path('release').exists(): time.sleep(.01)\")"
    )
    argv = (sys.executable, "-c", script, "")
    request = ProcessRequest(shlex.join(argv) if shell else argv, str(tmp_path))
    async with await LinuxProcessSupervisor().prepare() as owned:
        await owned.activate(request)
        task = asyncio.create_task(owned.wait())
        await _appears(tmp_path / "entered")
        for _ in range(5):
            await asyncio.sleep(0.01)
            assert not task.done()  # The real check is still blocked on our gate.
        (tmp_path / "release").touch()
        result = await task
    assert result.exit_code == 0
    assert result.stdout_tail.endswith("stdout-end")
    assert result.stderr_tail.endswith("stderr-end")
    assert len(result.stdout_tail) <= TAIL_BYTES
    assert len(result.stderr_tail) <= TAIL_BYTES
    assert result.cleanup == {"backend": "linux-subreaper", "echild": True}


_DETACHED = """
import os,pathlib,signal,time
if os.fork() == 0:
    os.setsid()
    if os.fork() != 0: os._exit(0)
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    pathlib.Path('helper.tmp').write_text(str(os.getpid()))
    os.replace('helper.tmp', 'helper')
    while True:
        with open('writes','a') as f: f.write('x')
        time.sleep(.005)
while not pathlib.Path('release').exists(): time.sleep(.005)
"""


@linux
@pytest.mark.asyncio
async def test_cancellation_during_real_activation_still_reaps_the_detached_writer(tmp_path, monkeypatch):
    from garuda.runtime.process_supervision import OwnedProcess

    entered, release = asyncio.Event(), asyncio.Event()
    original = OwnedProcess._message
    seen, handles = [], []

    async def gated_started(owned):
        message = await original(owned)
        if message["kind"] == "started":
            seen.append(message["command"])
            entered.set()
            await release.wait()
        return message

    monkeypatch.setattr(OwnedProcess, "_message", gated_started)
    owned = await LinuxProcessSupervisor().prepare()
    task = asyncio.create_task(owned.activate(ProcessRequest(
        (sys.executable, "-c", _DETACHED), str(tmp_path), timeout=10, term_grace=.1, kill_grace=.5)))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        handles.append(os.pidfd_open(seen[0]["pid"]))
        await _appears(tmp_path / "helper")
        pid = int((tmp_path / "helper").read_text())
        handles.append(os.pidfd_open(pid))
        await _appears(tmp_path / "writes")
        task.cancel()
        await asyncio.sleep(.02)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert owned.result.cleanup["echild"] is True
        assert owned.command == seen[0]
        assert not Path(f"/proc/{pid}").exists()
        count = (tmp_path / "writes").stat().st_size
        await asyncio.sleep(.05)
        assert (tmp_path / "writes").stat().st_size == count
        await owned.close()
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        try:
            await owned.close()
        except CleanupUncertain:
            pass
        for fd in handles:
            _signal(fd)


@linux
@pytest.mark.asyncio
@pytest.mark.parametrize("finish", ["leader-exit", "timeout", "cancel"])
async def test_double_forked_term_resistant_writer_is_reaped_before_return(tmp_path, finish):
    helper_fd = leader_fd = None
    unrelated = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(30)"],
                                 start_new_session=True)
    try:
        async with await LinuxProcessSupervisor().prepare() as owned:
            command = await owned.activate(ProcessRequest(
                (sys.executable, "-c", _DETACHED), str(tmp_path),
                timeout=2 if finish == "timeout" else 10, term_grace=0.1, kill_grace=0.5))
            leader_fd = os.pidfd_open(command["pid"])
            await _appears(tmp_path / "helper")
            helper_pid = int((tmp_path / "helper").read_text())
            helper_fd = os.pidfd_open(helper_pid)
            await _appears(tmp_path / "writes")
            task = asyncio.create_task(owned.wait())
            if finish == "leader-exit":
                (tmp_path / "release").touch()
            elif finish == "cancel":
                await asyncio.sleep(0)  # Enter wait() before cancelling that owner.
                task.cancel()
                await asyncio.sleep(0.02)
                task.cancel()  # Repeated cancellation cannot abandon owned cleanup.
            if finish == "cancel":
                with pytest.raises(asyncio.CancelledError):
                    await task
                result = owned.result
            else:
                result = await task
            assert result.outcome == {"leader-exit": "exited", "timeout": "timeout",
                                      "cancel": "cancelled"}[finish]
            assert result.cleanup["echild"] is True
            assert not Path(f"/proc/{helper_pid}").exists()
            count = (tmp_path / "writes").stat().st_size
            await asyncio.sleep(0.1)
            assert (tmp_path / "writes").stat().st_size == count
            assert unrelated.poll() is None
    finally:
        # These stable fixture handles also contain intentional gate mutations.
        for fd in (helper_fd, leader_fd):
            if fd is not None:
                _signal(fd)
        unrelated.kill()
        unrelated.wait(timeout=3)


@linux
@pytest.mark.asyncio
async def test_supervisor_death_cannot_claim_that_its_writer_stopped(tmp_path):
    fd = None
    owned = await LinuxProcessSupervisor().prepare()
    try:
        command = await owned.activate(ProcessRequest(
            (sys.executable, "-c", "import pathlib,time;pathlib.Path('entered').touch();time.sleep(30)"),
            str(tmp_path), timeout=30, term_grace=0.1, kill_grace=0.1))
        fd = os.pidfd_open(command["pid"])
        await _appears(tmp_path / "entered")
        owned.proc.kill()
        with pytest.raises(CleanupUncertain):
            await owned.wait()
        assert owned.result is None
        # Signal 0 through the original handle proves the writer remains alive.
        signal.pidfd_send_signal(fd, 0)
    finally:
        if fd is not None:
            _signal(fd)
        with pytest.raises(CleanupUncertain):
            await owned.close()


@linux
@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [float("nan"), float("inf"), True, 0, 901])
async def test_invalid_runtime_bound_cannot_activate_a_prepared_worker(tmp_path, limit):
    async with await LinuxProcessSupervisor().prepare() as owned:
        with pytest.raises(ValueError):
            await owned.activate(ProcessRequest("touch marker", str(tmp_path), timeout=limit))
    assert not (tmp_path / "marker").exists()
    assert owned.result.command is None
