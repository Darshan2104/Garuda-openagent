"""Local subprocess ownership across process groups in one issued POSIX session.

Debug adapters can give a launched debuggee its own process group. The session
leader remains unreaped until cleanup, preventing its session id from reuse.
Detached daemons that leave the session are outside this cleanup guardrail.
"""

import asyncio
import contextlib
import os
import signal
import subprocess
import time
import uuid

from garuda.runtime.ownership import Owner
from garuda.runtime.recovery import _process_identity, same_process
from garuda.workspace.settlement import settle_task


class CleanupUnproved(RuntimeError):
    pass


def session_groups(session_id: int) -> set[int]:
    """Observe groups containing live members; an unreadable table is unknown."""
    ps = next((p for p in ("/bin/ps", "/usr/bin/ps") if os.path.exists(p)), None)
    if ps is None:
        raise CleanupUnproved("cleanup unproved: ps unavailable")
    try:
        result = subprocess.run(
            [ps, "-Ao", "pid=,pgid=,stat="], capture_output=True, text=True,
            timeout=2, env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"},
        )
        if result.returncode or not result.stdout.strip():
            raise ValueError("process table unavailable")
        groups = set()
        for line in result.stdout.splitlines():
            pid, pgid, state = line.split()
            pid, pgid = int(pid), int(pgid)
            try:
                sid = os.getsid(pid)
            except ProcessLookupError:
                continue
            if sid == session_id and not state.startswith("Z"):
                groups.add(pgid)
        return groups
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise CleanupUnproved(f"cleanup unproved: {exc}") from exc


class OwnedProcessSession:
    """An environment-issued session, kept anchored by a live supervisor.

    The supervisor waits for stdin EOF after its child exits. Keeping it alive
    until signaling avoids reusing a dead adapter's session id during cleanup.
    """

    def __init__(self, process: asyncio.subprocess.Process):
        self.process = process
        self.owner = None
        self.started = False
        self._close_task = None

    def bind(self) -> None:
        pid = self.process.pid
        identity = _process_identity(pid)
        if not identity or os.getsid(pid) != pid or os.getpgid(pid) != pid:
            raise CleanupUnproved("cleanup unproved: cannot bind session supervisor identity")
        self.owner = Owner(pid, identity, pid, uuid.uuid4().hex)

    async def close(self) -> None:
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._close())
        try:
            await asyncio.shield(self._close_task)
        except asyncio.CancelledError as cancelled:
            try:
                await settle_task(self._close_task)
            except asyncio.CancelledError as exc:
                raise CleanupUnproved("cleanup unproved: session cleanup task was cancelled") from exc
            except Exception as exc:
                # The failed receipt is retained for the shared lease reaper;
                # preserve cancellation so a tool runner cannot resume the turn.
                raise cancelled from exc
            raise

    async def _close(self) -> None:
        process = self.process
        if not self.started:
            # The handshake has not released the supervisor to spawn an adapter.
            try:
                if process.returncode is None:
                    process.kill()
                await asyncio.wait_for(process.wait(), 2)
                return
            except (OSError, TimeoutError) as exc:
                raise CleanupUnproved(f"cleanup unproved: supervisor {process.pid}: {exc}") from exc
        # The unreaped direct child anchors the session throughout the kill.
        # asyncio reaps automatically, so loss of the supervisor before this
        # point cannot establish safe session-id ownership.
        if process.returncode is not None:
            raise CleanupUnproved("cleanup unproved: session supervisor exited early")
        sid = process.pid
        try:
            identity = _process_identity(sid)
            if (not self.owner or not identity or not same_process(self.owner.identity, identity)
                    or os.getsid(sid) != sid or os.getpgid(sid) != sid):
                raise CleanupUnproved("cleanup unproved: session identity changed")
            groups = await asyncio.to_thread(session_groups, sid)
            if sid not in groups:
                raise CleanupUnproved("cleanup unproved: live supervisor missing from process table")
            # Keep the supervisor alive while stopping every other group. Its
            # own group also includes the adapter, so stop that last.
            for group in groups - {sid}:
                try:
                    if os.getsid(group) != sid:
                        raise CleanupUnproved("cleanup unproved: process group identity changed")
                    os.killpg(group, signal.SIGKILL)
                except ProcessLookupError:
                    # A group can retain descendants after its leader exits.
                    # Reobserve membership under the still-live session anchor.
                    if group in await asyncio.to_thread(session_groups, sid):
                        with contextlib.suppress(ProcessLookupError):
                            os.killpg(group, signal.SIGKILL)
            os.killpg(sid, signal.SIGKILL)
            deadline = time.monotonic() + 3
            while await asyncio.to_thread(session_groups, sid):
                if time.monotonic() >= deadline:
                    raise CleanupUnproved("cleanup unproved: live session members remain")
                await asyncio.sleep(0.05)
            await asyncio.wait_for(process.wait(), 2)
        except (OSError, TimeoutError) as exc:
            raise CleanupUnproved(f"cleanup unproved: {exc}") from exc
