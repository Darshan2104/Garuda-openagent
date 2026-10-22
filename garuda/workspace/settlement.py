"""Retain ownership of launch and cleanup tasks after caller cancellation."""

import asyncio
from typing import TypeVar

_T = TypeVar("_T")


async def settle_task(task: asyncio.Task[_T]) -> _T:
    """Wait through repeated caller cancellation; retain the task's own outcome.

    Call only while handling cancellation: the owner must re-raise the original
    cancellation after settling its resources (and chain any settlement failure).
    """
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
    return task.result()
