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
