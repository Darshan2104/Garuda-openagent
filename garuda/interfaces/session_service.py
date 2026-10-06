"""One session lifecycle for every native entry point (plan task B.6, #157).

`garuda run` (through `run_agent_task`), `garuda chat`, dashboard chats and
the SDK `Conversation` each opened sessions their own way: different orders,
and some steps missing (no baseline, no approval broker, no reaping of
background processes). They now share these steps, in this order:

1. the caller resolves the effective configuration and role;
2. identity — the session id (and name);
3. workspace — :func:`choose_workspace` (shared, or a B.5 worktree);
4. capacity, then the workspace lease (``WorkspaceLeaseGuard`` gives the slot
   back when the workspace is held), then the session record and the
   baseline. A refused lease leaves no session record;
5. the approval broker, so every ask is parked, answered and audited alike;
6. the environment, then the runtime (the caller's agent loop);
7. outcome, delta and evidence — the caller's ``finish``;
8. :meth:`LiveSession.close` — reap background processes, tear the
   environment down, persist, and release the lease last. If any background
   process cannot be proven dead the session is *quarantined*: the lease stays
   held (heartbeat stopped) and the session records the pids, so nothing else
   edits the workspace while a stray writer may still be running.

A refused start leaves no lease, no capacity slot, no environment and no new
worktree behind; one refused after the session was recorded says so.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)


class SessionRefused(Exception):
    """A session could not start; nothing is left held. ``cause`` is the reason."""

    def __init__(self, cause: BaseException):
        super().__init__(str(cause))
        self.cause = cause


def choose_workspace(store, workspace, session_id, isolation="shared", workspace_kind="local", *,
                     resume=None, resume_all_projects=False):
    """Return ``(path, worktree meta or None)`` for a session (step 3, B.5).

    A resumed session goes back to the worktree it ran in. A new worktree's
    meta carries ``new: True`` so a refused launch can discard it.
    """
    from garuda.workspace.worktrees import WorktreeError, prepare_workspace

    if resume:
        if isolation != "shared":
            raise WorktreeError(
                "worktree.resume", "a resumed session keeps the workspace it ran in; drop --isolation"
            )
        try:
            prior = store.load_meta(
                store.resolve(resume, workspace=workspace, all_projects=resume_all_projects)
            )
        except Exception:
            return workspace, None  # resume resolution reports the error itself
        if prior.get("isolation") == "worktree" and prior.get("worktree"):
            if not os.path.isdir(prior["worktree"]):
                raise WorktreeError(
                    "worktree.missing", f"the session's worktree {prior['worktree']} is gone"
                )
            keys = ("isolation", "worktree", "branch", "source_repo", "source_head",
                    "dirty_source", "dirty_fingerprint")
            return prior["worktree"], {k: prior.get(k) for k in keys}
        return workspace, None
    if isolation == "shared":
        return workspace, None
    if workspace_kind != "local":
        raise WorktreeError(
            "worktree.unsupported_kind", f"--isolation {isolation} needs a local workspace"
        )
    plan = prepare_workspace(workspace, session_id, isolation)
    if plan.isolation == "shared":
        return workspace, None
    logger.warning(
        "Session %s works in worktree %s on branch %s%s", session_id, plan.path, plan.branch,
        "; uncommitted changes in the source checkout were not carried over"
        if plan.dirty_source else "",
    )
    return plan.path, {**plan.meta(), "new": True}


def discard_new_worktree(plan: dict | None) -> None:
    """Undo a worktree made for a session that was refused before it started."""
    if plan and plan.pop("new", False):
        from garuda.workspace.worktrees import discard_worktree

        discard_worktree(plan)


def plan_meta(plan: dict | None) -> dict:
    return {k: v for k, v in (plan or {}).items() if k != "new"}


def acquire_lease(workspace, session_id, *, capacity_key="native", mode="mutating",
                  worktree_plan=None, capability=None, capacity_loan=None):
    """Step 4a: capacity, then the lease. A refusal discards a new worktree.

    With a flow ``capability`` (C.6a) the step borrows its parent's lease for
    that exact workspace and takes only its own capacity slot.
    """
    import os

    from garuda.interfaces.run_guard import WorkspaceLeaseGuard
    from garuda.workspace.lease import LeaseError

    if capacity_loan is not None and capability is not None:
        raise LeaseError("capacity and workspace loans cannot be combined")
    if capability is not None:
        if os.path.realpath(str(workspace)) != os.path.realpath(capability.workspace):
            raise LeaseError("the lease capability is for another workspace")
        lease = capability.guard(capacity_key=capacity_key)
        lease.acquire()
        return lease
    lease = WorkspaceLeaseGuard(str(workspace), session_id, capacity_key=capacity_key, mode=mode,
                                capacity_loan=capacity_loan)
    try:
        lease.acquire()
    except BaseException:
        discard_new_worktree(worktree_plan)
        raise
    return lease


@dataclass
class LiveSession:
    """A started session: its lease, baseline, broker and environment."""

    store: Any
    session_id: str
    workspace: str
    workspace_kind: str
    lease: Any
    worktree: dict | None = None
    delta_loader: Callable | None = None
    broker: Any = None
    env: Any = None
    env_handle: Any = None
    quarantined: list[str] = field(default_factory=list)
    closed: bool = False

    async def turn(self, work: Awaitable[Any]) -> Any:
        """Run one turn against the lease heartbeat: a lost lease stops it."""
        return await self.lease.race(work)

    async def close(self, finish: Callable[[], Awaitable[None]] | None = None) -> list[str]:
        """Step 8. Returns the pids whose death is unproven (empty when clean)."""
        if self.closed:
            return self.quarantined
        self.closed = True
        survivors: list[str] = []
        try:
            if self.env is not None:
                from garuda.tools.background import reap_session_verified

                survivors = await reap_session_verified(self.session_id, self.env)
                if hasattr(self.env, "aclose"):
                    try:
                        await self.env.aclose()
                    except Exception:
                        logger.warning("Failed to close persistent shell", exc_info=True)
            try:
                from garuda.interfaces.runner import cleanup_workspace

                await cleanup_workspace(self.env_handle)
            except Exception:
                logger.warning("Workspace cleanup failed", exc_info=True)
            if finish is not None:
                await finish()
        finally:
            if survivors:
                await self._quarantine(survivors)
            else:
                await self.lease.release()
        return survivors

    async def _quarantine(self, pids: list[str]) -> None:
        self.quarantined = list(pids)
        await quarantine(self.store, self.session_id, self.lease, pids)


async def quarantine(store, session_id: str, lease, pids: list[str]) -> None:
    """Keep the workspace held: background processes are not proven dead."""
    logger.error(
        "Session %s left processes that could not be proven dead (%s); the workspace "
        "lease stays held until they are gone", session_id, ", ".join(pids),
    )
    try:
        store.update_meta(session_id, {"quarantine": {
            "reason": "background processes not proven dead", "pids": list(pids),
        }})
    except Exception:
        logger.warning("Recording quarantine failed", exc_info=True)
    await lease.stop_heartbeat()


async def open_session(
    store,
    session_id: str,
    workspace: str,
    *,
    workspace_kind: str = "local",
    permissions=None,
    lease_mode: str = "mutating",
    capacity_key: str = "native",
    isolation: str = "shared",
    docker_image: str = "ubuntu:22.04",
    docker_host: str | None = None,
    begin: Callable[[str], None] | None = None,
    with_environment: bool = True,
    resolve_env: Callable | None = None,
) -> LiveSession:
    """Steps 3–6 for a held, multi-turn session. See the module docstring.

    ``begin(workspace)`` records the session (step 2) once its workspace is
    leased, as `run_agent_task` does: a workspace or capacity refusal leaves
    no session record, and any later refusal is recorded as a startup
    refusal. Every refusal raises :class:`SessionRefused` with nothing held.
    ``resolve_env`` replaces the environment factory (the entry point's own
    import of ``resolve_environment``).
    """
    from garuda.interfaces.run_guard import install_session_broker
    from garuda.interfaces.runner import resolve_environment
    from garuda.workspace import evidence

    path, worktree = choose_workspace(store, workspace, session_id, isolation, workspace_kind)
    try:
        lease = acquire_lease(path, session_id, capacity_key=capacity_key, mode=lease_mode,
                              worktree_plan=worktree)
    except Exception as exc:
        raise SessionRefused(exc) from exc
    live = LiveSession(store=store, session_id=session_id, workspace=str(path),
                       workspace_kind=workspace_kind, lease=lease, worktree=plan_meta(worktree))
    try:
        if begin is not None:
            begin(str(path))
        lease.start_heartbeat()
        if worktree:
            store.update_meta(session_id, live.worktree)
        live.delta_loader = evidence.begin_session_evidence(store, session_id, path, workspace_kind)
        if permissions is not None:
            live.broker = install_session_broker(permissions, store, session_id)
        if with_environment:
            live.env, live.env_handle = await (resolve_env or resolve_environment)(
                workspace_kind, str(path), docker_image, docker_host=docker_host
            )
    except BaseException as exc:
        evidence.record_startup_refusal(store, session_id)
        await lease.release()
        discard_new_worktree(worktree)
        if isinstance(exc, Exception):
            raise SessionRefused(exc) from exc
        raise
    return live

