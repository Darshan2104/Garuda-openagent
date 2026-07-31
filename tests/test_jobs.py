"""C2: JobManager — submit/run/cancel/concurrency/retention."""

import asyncio

import pytest

from garuda.core.events import EventStore
from garuda.interfaces.jobs import JobManager, JobState
from garuda.types import AgentResult


def _result(success=True, msg="done", turns=1) -> AgentResult:
    return AgentResult(success=success, final_message=msg, messages=[], turns=turns)


async def test_job_runs_to_completion():
    mgr = JobManager(max_jobs=2)

    async def runner(job):
        return _result(msg="ok", turns=3)

    job = mgr.submit(runner, task="t", events=EventStore())
    assert job.state == JobState.QUEUED
    await job._task
    assert job.state == JobState.SUCCEEDED
    assert job.result.turns == 3 and job.result.final_message == "ok"


async def test_job_failure_is_captured():
    mgr = JobManager()

    async def runner(job):
        raise RuntimeError("boom")

    job = mgr.submit(runner, task="t", events=EventStore())
    await job._task
    assert job.state == JobState.FAILED
    assert "RuntimeError" in job.error and "boom" in job.error


async def test_job_cancel_while_running():
    mgr = JobManager()
    started = asyncio.Event()

    async def runner(job):
        started.set()
        await asyncio.sleep(10)
        return _result()

    job = mgr.submit(runner, task="t", events=EventStore())
    await started.wait()
    assert mgr.cancel(job.id) is True
    with pytest.raises(asyncio.CancelledError):
        await job._task
    assert job.state == JobState.CANCELLED


async def test_job_cancel_while_queued():
    mgr = JobManager(max_jobs=1)
    release = asyncio.Event()
    ran: list[str] = []

    async def blocker(job):
        ran.append("blocker-started")
        await release.wait()
        return _result()

    async def queued_runner(job):
        ran.append("queued-ran")  # must never happen: cancelled before its slot frees
        return _result()

    holder = mgr.submit(blocker, task="holder", events=EventStore())
    queued = mgr.submit(queued_runner, task="queued", events=EventStore())
    await asyncio.sleep(0.02)  # let the holder acquire the single slot
    assert holder.state == JobState.RUNNING
    assert queued.state == JobState.QUEUED

    assert mgr.cancel(queued.id) is True
    with pytest.raises(asyncio.CancelledError):
        await queued._task
    assert queued.state == JobState.CANCELLED
    assert "queued-ran" not in ran  # never got the slot

    release.set()
    await holder._task
    assert holder.state == JobState.SUCCEEDED


async def test_concurrency_cap_queues_excess():
    mgr = JobManager(max_jobs=1)
    release = asyncio.Event()
    running = 0
    peak = 0

    async def runner(job):
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await release.wait()
        running -= 1
        return _result()

    j1 = mgr.submit(runner, task="a", events=EventStore())
    j2 = mgr.submit(runner, task="b", events=EventStore())
    await asyncio.sleep(0.02)  # let j1 acquire the single slot
    assert j1.state == JobState.RUNNING
    assert j2.state == JobState.QUEUED  # gated by the semaphore
    release.set()
    await asyncio.gather(j1._task, j2._task)
    assert peak == 1  # never more than one ran at once
    assert j1.state == j2.state == JobState.SUCCEEDED


async def test_cancel_unknown_or_done_returns_false():
    mgr = JobManager()

    async def runner(job):
        return _result()

    job = mgr.submit(runner, task="t", events=EventStore())
    await job._task
    assert mgr.cancel(job.id) is False  # already terminal
    assert mgr.cancel("nope") is False


async def test_prune_evicts_old_terminal_jobs():
    mgr = JobManager(max_jobs=8, max_retained=3)

    async def runner(job):
        return _result()

    jobs = [mgr.submit(runner, task=str(i), events=EventStore()) for i in range(6)]
    await asyncio.gather(*[j._task for j in jobs])
    # trigger one more prune pass via a new submission
    extra = mgr.submit(runner, task="x", events=EventStore())
    await extra._task
    assert len(mgr.list()) <= 3


# --- retention: a quiet server must not hold every event history it ever saw --


async def test_the_cap_applies_when_jobs_finish_not_only_when_they_are_submitted():
    """The leak: pruning only at submit time is unbounded in wall-clock.

    A server that takes a burst of jobs and then goes quiet — the normal state of
    a long-lived server, not an edge case — retains every completed job's full
    in-memory event history until something else is submitted, which may be never.
    """
    mgr = JobManager(max_jobs=8, max_retained=2)

    async def runner(job):
        return _result()

    jobs = [mgr.submit(runner, task=str(i), events=EventStore()) for i in range(6)]
    await asyncio.gather(*[j._task for j in jobs])
    # No further submission: retention has to hold on its own.
    assert len(mgr.list()) <= 2


async def test_terminal_jobs_expire_after_their_ttl():
    mgr = JobManager(retain_seconds=60.0)

    async def runner(job):
        return _result()

    job = mgr.submit(runner, task="t", events=EventStore())
    await job._task
    assert mgr.get(job.id) is not None

    job.finished_at -= 61.0  # as if the result went uncollected for an hour
    assert mgr.get(job.id) is None
    assert mgr.list() == []


async def test_a_running_job_is_never_evicted():
    """Eviction is for results nobody collected, never for work in flight."""
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow(job):
        started.set()
        await release.wait()
        return _result()

    async def fast(job):
        return _result()

    mgr = JobManager(max_jobs=8, max_retained=1, retain_seconds=0.0)
    long_job = mgr.submit(slow, task="slow", events=EventStore())
    await started.wait()

    others = [mgr.submit(fast, task=str(i), events=EventStore()) for i in range(5)]
    await asyncio.gather(*[j._task for j in others])

    assert mgr.get(long_job.id) is not None
    release.set()
    await long_job._task


async def test_ttl_of_zero_keeps_the_count_cap_only():
    mgr = JobManager(max_retained=5, retain_seconds=0.0)

    async def runner(job):
        return _result()

    job = mgr.submit(runner, task="t", events=EventStore())
    await job._task
    job.finished_at -= 10_000.0
    assert mgr.get(job.id) is not None


async def test_a_cancelled_job_is_stamped_and_becomes_evictable():
    release = asyncio.Event()

    async def slow(job):
        await release.wait()
        return _result()

    mgr = JobManager(retain_seconds=60.0)
    job = mgr.submit(slow, task="t", events=EventStore())
    await asyncio.sleep(0)
    assert mgr.cancel(job.id) is True
    with pytest.raises(asyncio.CancelledError):
        await job._task

    assert job.finished_at is not None
    job.finished_at -= 61.0
    assert mgr.get(job.id) is None
