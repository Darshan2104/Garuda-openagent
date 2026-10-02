"""Run invariants shared by every facade that mutates a workspace.

The native loop (`runner.run_agent_task`), the ACP launch path
(`garuda run --runtime <acp>`), and a confirmed CLI handoff all hold the same
guarantees, so they share one implementation instead of copies:

- `WorkspaceLeaseGuard` — the single mutating workspace lease (P0.16):
  acquire before any session state or process, heartbeat while work runs,
  race the work against the heartbeat so a lost lease stops it, and release
  on every path including cancellation.
- `approval_answerer` / `install_session_broker` / `broker_approval_handler`
  — the P0.17 approval path: every ask parks in an `ApprovalBroker` and is
  audited; a caller-supplied interactive handler becomes the answerer, and
  with none the broker denies immediately (still audited) instead of hanging.

This is a guardrail around the run, not a sandbox boundary.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

logger = logging.getLogger(__name__)


def lease_mode_for(permission_mode: str | None) -> str:
    """A read-only permission posture takes a shared read-only lease."""
    return "read-only" if permission_mode == "readonly" else "mutating"


class WorkspaceLeaseGuard:
    """Hold one mutating workspace lease for the length of a facade call."""

    def __init__(
        self,
        workspace: str,
        session_id: str,
        *,
        lease_store=None,
        ttl_sec=None,
        capacity_key: str | None = None,
        capacity_store=None,
        capacity_ceiling: int | None = None,
        mode: str = "mutating",
    ):
        from garuda.workspace.lease import DEFAULT_TTL_SEC, LeaseStore

        self.workspace = workspace
        self.session_id = session_id
        self.leases = lease_store if lease_store is not None else LeaseStore()
        self._ttl = DEFAULT_TTL_SEC if ttl_sec is None else ttl_sec
        self._heartbeat: asyncio.Future | None = None
        self._held = False
        self._epoch: str | None = None
        # The runtime/provider this launch draws capacity from (B.0). The
        # ceiling comes from the user's global settings unless passed in.
        self._capacity_key = capacity_key
        self._capacity_store = capacity_store
        self._capacity_ceiling = capacity_ceiling
        self._reservation = None
        # "read-only" work shares the workspace with other readers and never
        # blocks or is blocked by them; it still conflicts with nothing else.
        self._mode = mode

    def acquire(self) -> None:
        """Reserve runtime capacity, then take the mutating lease.

        Capacity first and the workspace second, never waiting on either: if
        no slot is free the run refuses (`CapacityUnavailable`), and if the
        workspace is held the slot is given back before the conflict
        propagates. `LeaseConflictError`/`LeaseError` never degrade to unlocked.
        """
        self._reserve_capacity()
        try:
            lease = self.leases.acquire(self.workspace, self.session_id, mode=self._mode)
        except BaseException:
            self._release_capacity()
            raise
        self._epoch = getattr(lease, "epoch", None) or None
        self._held = True

    def _reserve_capacity(self) -> None:
        if not self._capacity_key:
            return
        from garuda.runtime.capacity import CapacityStore, configured_ceiling

        ceiling = self._capacity_ceiling
        if ceiling is None:
            ceiling = configured_ceiling(self._capacity_key)
        if ceiling is None:
            return
        store = self._capacity_store or CapacityStore()
        self._reservation = store.reserve(self._capacity_key, self.session_id, ceiling)
        self._capacity_store = store

    def _release_capacity(self) -> None:
        reservation, self._reservation = self._reservation, None
        if reservation is None or self._capacity_store is None:
            return
        try:
            self._capacity_store.release(reservation)
        except Exception:
            logger.warning("Capacity release failed", exc_info=True)

    def start_heartbeat(self) -> None:
        if self._heartbeat is not None:
            return

        async def _beat() -> None:
            while True:
                await asyncio.sleep(self._ttl / 3)
                # Losing the lease means the run can no longer prove exclusive
                # mutation authority; the error ends `race` below.
                self.leases.heartbeat(self.workspace, self.session_id, epoch=self._epoch)

        self._heartbeat = asyncio.ensure_future(_beat())

    async def race(self, work: Awaitable[Any]) -> Any:
        """Await `work` while the heartbeat holds; a lost lease cancels it.

        Cancelling the caller cancels `work` too: `asyncio.wait` does not, and
        work outliving teardown would mutate after the lease is released.
        """
        task = asyncio.ensure_future(work)
        if self._heartbeat is None:
            return await task
        try:
            done, _ = await asyncio.wait(
                {task, self._heartbeat}, return_when=asyncio.FIRST_COMPLETED
            )
        except asyncio.CancelledError:
            await _cancel_quietly(task)
            raise
        if self._heartbeat in done:
            error = self._heartbeat.exception()
            await _cancel_quietly(task)
            if error is None:
                from garuda.workspace.lease import LeaseError

                raise LeaseError("lease heartbeat stopped unexpectedly")
            raise error
        return await task

    async def _stop_beating(self) -> None:
        heartbeat, self._heartbeat = self._heartbeat, None
        if heartbeat is not None:
            heartbeat.cancel()
            try:
                await heartbeat
            except (asyncio.CancelledError, Exception):
                pass

    async def stop_heartbeat(self) -> None:
        """Quarantine (B.6): keep the lease, stop renewing it, give the slot back.

        The expired lease is taken over only once this process is confirmed
        dead, so the workspace stays held while possible stray writers remain.
        """
        await self._stop_beating()
        self._release_capacity()

    async def release(self) -> None:
        """Stop the heartbeat and release. Best-effort; never masks an error."""
        await self._stop_beating()
        try:
            if not self._held:
                return
            self._held = False
            try:
                self.leases.release(self.workspace, self.session_id, epoch=self._epoch)
            except Exception:
                logger.warning("Lease release failed", exc_info=True)
        finally:
            self._release_capacity()


async def _cancel_quietly(task: asyncio.Future) -> None:
    if not task.done():
        task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass


def approval_answerer(
    handler: Callable[[str], Awaitable[bool]] | None,
) -> Callable[[Any], Awaitable[bool]]:
    """The broker answerer for a run: `handler` when given, else deny-all.

    A failing handler denies rather than hangs; either way the broker audits
    the outcome.
    """
    if handler is None:
        async def _deny_all(_request) -> bool:
            return False

        return _deny_all

    async def _answer(request) -> bool:
        try:
            return bool(await handler(request.action))
        except Exception:
            logger.warning("Approval answerer failed; denying", exc_info=True)
            return False

    return _answer


def session_approval_channel(store, session_id: str):
    """The session's file approval channel (B.8), or ``None`` without a store."""
    if store is None or not session_id:
        return None
    from garuda.acp.approval_channel import FileApprovalChannel

    return FileApprovalChannel(store.session_dir(session_id) / "approvals", session_id)


def install_session_broker(permissions, store, session_id: str):
    """Route a native engine's ASK decisions through the session broker.

    The engine's existing interactive handler (if any) becomes the answerer,
    so it is never bypassed; returns the broker.
    """
    from garuda.acp.broker import ApprovalBroker

    broker = ApprovalBroker(engine=permissions, store=store,
                            channel=session_approval_channel(store, session_id))
    broker.set_answerer(approval_answerer(permissions.approval_handler))
    permissions.install_approval_handler(broker.handler(session_id=session_id))
    return broker


def interactive_approval() -> Callable[[str], Awaitable[bool]] | None:
    """A y/N terminal prompt when stdin and stderr are TTYs, else None.

    None means the headless posture: the broker denies every ask (audited).
    """
    import sys

    try:
        interactive = sys.stdin.isatty() and sys.stderr.isatty()
    except (AttributeError, ValueError):
        interactive = False
    if not interactive:
        return None
    import functools

    from garuda.interfaces.cli import stdin_approval

    return functools.partial(stdin_approval, stream=sys.stderr)


def broker_approval_handler(
    store,
    session_id: str,
    runtime_id: str,
    *,
    handler: Callable[[str], Awaitable[bool]] | None = None,
    timeout_sec: float = 300.0,
):
    """An ACP `approval_handler` that parks every agent request in a broker.

    `session/request_permission` asks from an external runtime get the same
    park/answer/timeout/audit lifecycle as native asks. Returns
    `(handler, broker)`.
    """
    from garuda.acp.broker import ApprovalBroker
    from garuda.core.permissions import PermissionEngine

    broker = ApprovalBroker(
        engine=PermissionEngine(), store=store, timeout_sec=timeout_sec,
        channel=session_approval_channel(store, session_id),
    )
    broker.set_answerer(approval_answerer(handler))
    return broker.handler(session_id=session_id, runtime_id=runtime_id), broker


__all__ = [
    "WorkspaceLeaseGuard",
    "session_approval_channel",
    "approval_answerer",
    "broker_approval_handler",
    "install_session_broker",
    "interactive_approval",
]
