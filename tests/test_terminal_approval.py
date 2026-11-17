"""Terminal approval routing through the real broker and builtins.input."""

import asyncio
import functools
import io
import queue
import threading

import pytest

from garuda.acp.broker import ApprovalBroker, ApprovalRequest
from garuda.core.permissions import PermissionEngine
from garuda.core.sessions import SessionStore
from garuda.interfaces.cli import stdin_approval
from garuda.interfaces.run_guard import approval_answerer


async def until(predicate):
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(0.005)


@pytest.fixture
def tty(monkeypatch):
    lines, lock = queue.Queue(), threading.Lock()

    def read():
        with lock:
            line = lines.get(timeout=5)
            if isinstance(line, BaseException):
                raise line
            return line

    monkeypatch.setattr('builtins.input', read)
    yield lines
    # Release any blocked input after a failed assertion, without hanging pytest.
    for _ in range(5):
        lines.put(EOFError())


async def test_concurrent_stdin_prompts_show_one_and_route_the_line_to_it(tty):
    out = io.StringIO()
    broker = ApprovalBroker(PermissionEngine(), answerer=approval_answerer(
        functools.partial(stdin_approval, stream=out)))
    first = asyncio.create_task(broker.handler()('rm -rf build'))
    second = asyncio.create_task(broker.handler()('ls'))
    try:
        await until(lambda: 'Approve' in out.getvalue())
        await asyncio.sleep(0.03)
        assert out.getvalue().count('Approve') == 1
        assert 'rm -rf build' in out.getvalue()
        tty.put('y')
        assert await asyncio.wait_for(first, 2)
        await until(lambda: out.getvalue().count('Approve') == 2)
        assert not second.done()
        tty.put('n')
        assert not await asyncio.wait_for(second, 2)
    finally:
        for task in (first, second):
            task.cancel()
        await asyncio.gather(first, second, return_exceptions=True)


@pytest.mark.parametrize('line_arrives_before_next_prompt', [False, True])
async def test_line_for_withdrawn_prompt_is_discarded_and_prompt_reshown(tty, line_arrives_before_next_prompt):
    out = io.StringIO()
    broker = ApprovalBroker(PermissionEngine(), timeout_sec=0.15,
                            answerer=approval_answerer(functools.partial(stdin_approval, stream=out)))
    assert not await broker.handler()('A')
    broker._timeout_sec = 2
    if line_arrives_before_next_prompt:
        tty.put('y')
        await until(tty.empty)
        await asyncio.sleep(0.03)
    second = asyncio.create_task(broker.handler()('B'))
    try:
        await until(lambda: 'Approve B' in out.getvalue())
        if not line_arrives_before_next_prompt:
            tty.put('y')
        await until(lambda: 'discarded input' in out.getvalue())
        assert out.getvalue().count('Approve B') == 2
        assert not second.done()
        tty.put('n')
        assert not await asyncio.wait_for(second, 2)
        assert 'approval withdrawn' in out.getvalue()
    finally:
        second.cancel()
        await asyncio.gather(second, return_exceptions=True)


async def test_prompt_labels_worker_agent_depth_and_queue_position(tty):
    from garuda.interfaces.terminal_approval import TerminalApprover

    out = io.StringIO()
    request = ApprovalRequest('id', 'bash(echo exact)', 'approval', 'native', 'root',
                              requester={'session_id': 'worker-123456789', 'agent': 'editor',
                                         'depth': 1, 'kind': 'editing_worker'},
                              queue_position=1, queue_length=2)
    task = asyncio.create_task(TerminalApprover(out).ask_request(request))
    try:
        await until(lambda: 'Approve?' in out.getvalue())
        prompt = out.getvalue()
        assert '1 of 2' in prompt and 'worker worker-12345' in prompt
        assert 'agent editor' in prompt and 'depth 1' in prompt
        assert request.action in prompt
        tty.put('yes')
        assert await task
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_eof_denies_as_disconnected(tty, tmp_path):
    store = SessionStore(tmp_path)
    store.begin('root', task='t', model='m', agent='a', workspace='w')
    broker = ApprovalBroker(PermissionEngine(), store=store,
                            answerer=approval_answerer(functools.partial(stdin_approval, stream=io.StringIO())))
    tty.put(EOFError())
    assert not await broker.handler(session_id='root')('A')
    records = [v for k, v in store.load_meta('root').items() if k.startswith('approval:')]
    assert records[0]['outcome'] == 'disconnected'


async def test_separate_brokers_share_terminal_presentation(tty):
    from garuda.interfaces.terminal_approval import TerminalApprover

    out = io.StringIO()
    brokers = [ApprovalBroker(PermissionEngine(), answerer=approval_answerer(TerminalApprover(out)))
               for _ in range(2)]
    tasks = [asyncio.create_task(broker.handler()(action))
             for broker, action in zip(brokers, ('A', 'B'), strict=True)]
    try:
        await until(lambda: 'Approve A' in out.getvalue())
        await asyncio.sleep(0.03)
        assert out.getvalue() == '\n[garuda] Approve A? [y/N]: '
        tty.put('y')
        assert await asyncio.wait_for(tasks[0], 2)
        await until(lambda: 'Approve B' in out.getvalue())
        tty.put('n')
        assert not await asyncio.wait_for(tasks[1], 2)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
