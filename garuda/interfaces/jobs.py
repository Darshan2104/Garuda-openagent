"""In-process job queue for the JSON-RPC server.

Turns the one-shot ``run`` (which holds the connection for the whole agent run)
into submit → poll/stream → result/cancel. Each job runs as a background asyncio
task on the server's loop, gated by a concurrency semaphore so a burst of
submissions doesn't launch unbounded agents at once.
"""

from __future__ import annotations

import asyncio
import logging
import time
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
    # Monotonic time the job reached a terminal state; None while it can still
    # run. Drives TTL eviction — a job's *age* is only meaningful once finished.
    finished_at: float | None = None
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
    """Owns submitted jobs and caps how many run concurrently.

    Retention is bounded on two axes because either alone leaks. A count cap
    applied only at submit time is unbounded in wall-clock: a server that takes
    ten jobs and then goes quiet holds ten full event histories forever, and on a
    long-lived server "then goes quiet" is the normal case, not the edge one. So
    the cap is also enforced when a job finishes, and terminal jobs additionally
    expire — a result nobody has collected in an hour is not being collected.
    """

    def __init__(
        self, max_jobs: int = 4, max_retained: int = 200, retain_seconds: float = 3600.0
    ):
        self._jobs: dict[str, Job] = {}
        self._order: list[str] = []
        self._sem = asyncio.Semaphore(max(1, max_jobs))
        self._max_retained = max_retained
        self._retain_seconds = retain_seconds

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
        finally:
            # Reached on the cancel path too, before CancelledError propagates.
            job.finished_at = time.monotonic()
            self._prune()

    def get(self, job_id: str) -> Job | None:
        self._prune()
        return self._jobs.get(job_id)

    def list(self) -> list[Job]:
        self._prune()
        return [self._jobs[j] for j in self._order if j in self._jobs]

    def cancel(self, job_id: str) -> bool:
        job = self._jobs.get(job_id)
        if job is None or job.done:
            return False
        if job._task is not None:
            job._task.cancel()
        return True

    def _prune(self) -> None:
        """Evict expired and surplus terminal jobs. Running jobs are never evicted."""
        to_remove = self._expired()
        surplus = len(self._order) - len(to_remove) - self._max_retained
        if surplus > 0:
            removable = [
                jid
                for jid in self._order
                if jid not in to_remove and jid in self._jobs and self._jobs[jid].done
            ]
            to_remove.update(removable[:surplus])
        if not to_remove:
            return
        keep: list[str] = []
        for jid in self._order:
            if jid in to_remove:
                self._jobs.pop(jid, None)
            else:
                keep.append(jid)
        self._order = keep

    def _expired(self) -> set[str]:
        """Terminal jobs whose results have gone uncollected past the TTL."""
        if not self._retain_seconds:
            return set()
        cutoff = time.monotonic() - self._retain_seconds
        return {
            jid
            for jid in self._order
            if (job := self._jobs.get(jid)) is not None
            and job.finished_at is not None
            and job.finished_at < cutoff
        }
