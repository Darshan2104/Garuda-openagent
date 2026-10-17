"""Select a role parent's workspace and settle refused worktree cleanup."""

from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager


async def _settled(task):
    cancelled = False
    while True:
        try:
            return await asyncio.shield(task), cancelled
        except asyncio.CancelledError:
            cancelled = True
            if task.done():
                return task.result(), cancelled


@asynccontextmanager
async def run_workspace(store, source, session_id, isolation, *, reserved_background=False):
    """Never abandon a creating worker or discard an admitted workspace.

    A partial session directory is retained conservatively: publication or
    execution may have begun. Only a newly selected, never-admitted tree is
    eligible for automatic removal.
    """
    from garuda.interfaces.session_service import choose_workspace, discard_new_worktree

    def select():
        path, worktree = choose_workspace(store, source, session_id, isolation)
        # If identity cannot be captured, leave the selection for inspection:
        # removal cannot safely assume a vanished/replaced path is ours.
        info = os.stat(path)
        return path, worktree, [info.st_dev, info.st_ino]

    (path, worktree, identity), cancelled = await _settled(
        asyncio.create_task(asyncio.to_thread(select)))
    try:
        if cancelled:
            raise asyncio.CancelledError
        yield path, worktree, identity
    finally:
        absent = not os.path.lexists(store.session_dir(session_id))
        if reserved_background and not absent:
            # A queued parent exists before workspace selection. Only a proven
            # never-admitted worktree may be discarded after a refusal.
            from garuda.workspace.lease import LeaseStore

            meta = store.load_meta(session_id)
            absent = (meta.get("flow_operation") is None
                      and not (store.session_dir(session_id) / "flow").exists()
                      and not LeaseStore().live_holders_for_session(session_id))
        if worktree and absent:
            _, interrupted = await _settled(asyncio.create_task(
                asyncio.to_thread(discard_new_worktree, worktree)))
            if interrupted:
                raise asyncio.CancelledError
