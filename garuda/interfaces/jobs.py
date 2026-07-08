"""In-process job queue for the JSON-RPC server.

Turns the one-shot ``run`` (which holds the connection for the whole agent run)
into submit → poll/stream → result/cancel. Each job runs as a background asyncio
task on the server's loop, gated by a concurrency semaphore so a burst of
submissions doesn't launch unbounded agents at once.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import Enum

from garuda.core.events import EventStore
from garuda.types import AgentResult

logger = logging.getLogger(__name__)


class JobState(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


_TERMINAL = {JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED}


@dataclass
class Job:
    id: str
    task: str
    events: EventStore
    state: JobState = JobState.QUEUED
    result: AgentResult | None = None
    error: str | None = None
    _task: asyncio.Task | None = field(default=None, repr=False)

    @property
    def done(self) -> bool:
        return self.state in _TERMINAL

    @property
    def session_id(self) -> str:
        return self.events.session_id


# A factory that, given the job, produces the awaitable performing the run.
JobRunner = Callable[[Job], Awaitable[AgentResult]]


class JobManager:
    """Owns submitted jobs and caps how many run concurrently."""

    def __init__(self, max_jobs: int = 4, max_retained: int = 200):
        self._jobs: dict[str, Job] = {}
        self._order: list[str] = []
        self._sem = asyncio.Semaphore(max(1, max_jobs))
        self._max_retained = max_retained

    def submit(self, runner: JobRunner, *, task: str, events: EventStore) -> Job:
        job = Job(id=uuid.uuid4().hex, task=task, events=events)
        self._jobs[job.id] = job
        self._order.append(job.id)
        job._task = asyncio.create_task(self._run_job(job, runner))
        self._prune()
        return job

    async def _run_job(self, job: Job, runner: JobRunner) -> None:
        try:
            # Stays QUEUED until a concurrency slot frees. A cancel while queued
            # raises CancelledError right at this await (before the body below
            # ever runs) -> handled by the except clause as CANCELLED.
            async with self._sem:
                job.state = JobState.RUNNING
                job.result = await runner(job)
                job.state = JobState.SUCCEEDED
        except asyncio.CancelledError:
            job.state = JobState.CANCELLED
            raise
        except Exception as exc:  # infra/agent error — task itself failed
            job.state = JobState.FAILED
            job.error = f"{type(exc).__name__}: {exc}"
            logger.warning("Job %s failed", job.id, exc_info=True)

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def list(self) -> list[Job]:
        return [self._jobs[j] for j in self._order if j in self._jobs]

    def cancel(self, job_id: str) -> bool:
        job = self._jobs.get(job_id)
        if job is None or job.done:
            return False
        if job._task is not None:
            job._task.cancel()
        return True

    def _prune(self) -> None:
        """Evict oldest terminal jobs beyond the retention cap (running ones stay)."""
        if len(self._order) <= self._max_retained:
            return
        keep: list[str] = []
        removable = [
            jid
            for jid in self._order
            if jid in self._jobs and self._jobs[jid].done
        ]
        to_remove = set(removable[: max(0, len(self._order) - self._max_retained)])
        for jid in self._order:
            if jid in to_remove:
                self._jobs.pop(jid, None)
            else:
                keep.append(jid)
        self._order = keep
