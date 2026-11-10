
import pytest

from garuda.tools.files import ReadFileTool
from garuda.tools.protocol import ToolContext
from garuda.workspace.health import (
    CONSECUTIVE_FAILURE_LIMIT,
    EnvironmentUnavailableError,
    monitored,
)
from garuda.workspace.local import LocalEnvironment


@pytest.mark.asyncio
async def test_page_does_not_use_whole_file_read(tmp_path, monkeypatch):
    import garuda.workspace.text_reads as reads

    monkeypatch.setattr(reads, 'SCAN_BLOCK_BYTES', reads.CHUNK_BYTES)
    path = tmp_path / 'large.txt'
    with path.open('w') as f:
        for _ in range(30000):
            f.write('hello world\n')
    env = LocalEnvironment(tmp_path)

    async def forbidden(*args):
        pytest.fail('whole-file read used for bounded page')

    monkeypatch.setattr(env, 'read_file', forbidden)
    result = await ReadFileTool().execute(
        {'path': path.name, 'offset': 20000, 'limit': 2}, env, ToolContext(session_id='test'))
    assert not result.is_error
    assert '20000\thello world' in result.content
    assert result.metadata['bounded_read'] is True
    assert result.metadata['bytes_read'] < path.stat().st_size


@pytest.mark.asyncio
@pytest.mark.parametrize('text', ['', 'a\r\nb\rc\n', 'α\vβ\x85γ\u2028δ\u2029', 'a\n\n', 'x'])
async def test_unicode_line_semantics_and_tail(tmp_path, text):
    (tmp_path / 'a').write_bytes(text.encode())
    env = LocalEnvironment(tmp_path)
    lines = text.splitlines()
    page = await env.read_text_page('a', limit=2, tail=True)
    assert page.lines == tuple(lines[-2:])
    assert page.offset == max(1, len(lines) - 1)
    assert page.total_lines == len(lines)


@pytest.mark.asyncio
async def test_long_lines_have_bounded_allocation(tmp_path, monkeypatch):
    import tracemalloc

    import garuda.workspace.text_reads as reads

    monkeypatch.setattr(reads, 'SCAN_BLOCK_BYTES', 64 * 1024)
    path = tmp_path / 'long'
    with path.open('wb') as f:
        for _ in range(500):
            f.write(b'x' * 8192)
        f.write(b'\nend\n')
    tracemalloc.start()
    try:
        page = await LocalEnvironment(tmp_path).read_text_page('long', limit=1)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert len(page.lines[0]) == 2001
    assert peak < 1024 * 1024  # input is over 4 MiB; no full allocation
    result = await ReadFileTool().execute(
        {'path': 'long', 'limit': 1}, LocalEnvironment(tmp_path), ToolContext(session_id='test'))
    assert '(line truncated)' in result.content


@pytest.mark.asyncio
async def test_actual_read_sizes_and_unknown_totals(tmp_path, monkeypatch):
    import os

    import garuda.workspace.text_reads as reads
    from garuda.workspace.text_reads import CHUNK_BYTES

    monkeypatch.setattr(reads, 'SCAN_BLOCK_BYTES', CHUNK_BYTES)
    path = tmp_path / 'large'
    path.write_bytes(b'row\n' * 50000)
    original = os.fdopen
    sizes = []

    class Reader:
        def __init__(self, *args, **kwargs):
            self.inner = original(*args, **kwargs)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.inner.close()

        def fileno(self):
            return self.inner.fileno()

        def read(self, size):
            assert 0 < size <= CHUNK_BYTES
            result = self.inner.read(size)
            sizes.append(len(result))
            return result

    monkeypatch.setattr(os, 'fdopen', Reader)
    page = await LocalEnvironment(tmp_path).read_text_page('large', offset=20000, limit=2)
    assert sum(sizes) == page.bytes_read < path.stat().st_size
    assert page.total_lines is None and page.has_more
    assert page.lines == ('row', 'row')


@pytest.mark.asyncio
async def test_large_file_offset_and_tail_reads_succeed_with_bounded_tail_io(tmp_path, monkeypatch):
    import garuda.workspace.text_reads as reads

    # Shrink any total scan ceiling: a late offset must still read like the
    # whole-file path did, and a tail must seek from EOF instead of scanning.
    monkeypatch.setattr(reads, 'MAX_SCAN_BYTES', 8192, raising=False)
    path = tmp_path / 'big.log'
    with path.open('w') as f:
        for i in range(1, 300001):
            f.write(f'line {i}\n')
    size = path.stat().st_size
    assert size > 2 * 1024 * 1024
    ctx = ToolContext(session_id='test')
    env = monitored(LocalEnvironment(tmp_path))
    page = await ReadFileTool().execute({'path': 'big.log', 'offset': 250000, 'limit': 2}, env, ctx)
    assert not page.is_error, page.content
    assert page.content.startswith('250000\tline 250000\n250001\tline 250001')

    tail = await ReadFileTool().execute({'path': 'big.log', 'tail': True, 'limit': 3}, env, ctx)
    assert not tail.is_error, tail.content
    assert tail.content.startswith('line 299998\nline 299999\nline 300000\n')
    assert 'line numbers and total lines unknown' in tail.content
    assert tail.metadata['total_lines'] is None
    assert tail.metadata['bytes_read'] <= 2 * reads.CHUNK_BYTES < size
    assert env.is_alive


@pytest.mark.asyncio
async def test_tail_seek_handles_breaks_across_blocks(tmp_path, monkeypatch):
    import random

    import garuda.workspace.text_reads as reads

    # Tiny blocks put CRLF pairs and multi-byte separators across every
    # backward block boundary; tails must still match str.splitlines.
    monkeypatch.setattr(reads, 'CHUNK_BYTES', 3)
    monkeypatch.setattr(reads, 'EXACT_TAIL_BYTES', 0)
    rng = random.Random(404)
    env = LocalEnvironment(tmp_path)
    for _ in range(300):
        text = ''.join(rng.choice(['a', 'β', '\n', '\r', '\r\n', '\x85', '\u2028', '\v'])
                       for _ in range(rng.randrange(1, 30)))
        (tmp_path / 'a').write_bytes(text.encode())
        expected = text.splitlines()
        limit = rng.randrange(1, 6)
        page = await env.read_text_page('a', limit=limit, tail=True)
        assert page.lines == tuple(expected[-limit:]), (text, limit)
        if page.offset is None:
            assert page.total_lines is None and len(expected) > limit
        else:
            assert page.total_lines == len(expected)


@pytest.mark.asyncio
async def test_chunked_forward_scan_matches_splitlines(tmp_path, monkeypatch):
    import random

    import garuda.workspace.text_reads as reads

    rng = random.Random(4040)
    env = LocalEnvironment(tmp_path)
    pieces = ['a', 'β', '€', '\n', '\r', '\r\n', '\x0b', '\x0c', '\x1c', '\x1d', '\x1e',
              '\x85', '\u2028', '\u2029', 'z' * 2003]
    for _ in range(400):
        # Tiny blocks split CRLF pairs, multi-byte characters and long lines.
        monkeypatch.setattr(reads, 'SCAN_BLOCK_BYTES', rng.randrange(1, 6))
        text = ''.join(rng.choice(pieces) for _ in range(rng.randrange(0, 25)))
        (tmp_path / 'a').write_bytes(text.encode())
        lines = text.splitlines()
        offset, limit = rng.randrange(1, len(lines) + 3), rng.randrange(1, 5)
        page = await env.read_text_page('a', offset=offset, limit=limit)
        expected = tuple(line[:2001] for line in lines[offset - 1:offset - 1 + limit])
        assert (page.lines, page.offset) == (expected, offset), ascii(text[:80])
        if page.total_lines is None:
            # Existing contract: only files of at least one chunk stop early.
            assert len(text.encode()) >= reads.CHUNK_BYTES and len(lines) >= offset + limit
            assert page.has_more
        else:
            assert page.total_lines == len(lines)
            assert page.has_more == (len(lines) >= offset + len(expected))
        tail = await env.read_text_page('a', limit=limit, tail=True)
        assert tail.lines == tuple(line[:2001] for line in lines[-limit:]), ascii(text[:80])
        assert tail.total_lines == len(lines)


@pytest.mark.asyncio
async def test_missing_file_and_invalid_tail(tmp_path):
    (tmp_path / 'small').write_bytes(b'x' * 20000)
    env = LocalEnvironment(tmp_path)
    with pytest.raises(FileNotFoundError):
        await env.read_text_page('missing')
    with pytest.raises(ValueError):
        await env.read_text_page('small', offset=2, tail=True)


@pytest.mark.asyncio
async def test_unsupported_backend_keeps_legacy_but_refuses_required_reads():
    class Legacy:
        async def read_file(self, path):
            return 'legacy\n'

    ctx = ToolContext(session_id='test')
    tool = ReadFileTool()
    assert not (await tool.execute({'path': 'a'}, Legacy(), ctx)).is_error
    for options in ({'bounded': True}, {'tail': True}):
        result = await tool.execute({'path': 'a', **options}, Legacy(), ctx)
        assert result.is_error and 'not supported' in result.content


@pytest.mark.asyncio
async def test_health_monitor_routes_bounded_reads_through_its_latch(tmp_path):
    class Legacy:
        workspace_root = str(tmp_path)

        async def read_file(self, path):
            return 'legacy\n'

    ctx = ToolContext(session_id='test')
    tool = ReadFileTool()
    (tmp_path / 'a').write_text('one\ntwo\n', encoding='utf-8')
    local = monitored(LocalEnvironment(workspace_root=str(tmp_path)))
    result = await tool.execute({'path': 'a', 'tail': True, 'limit': 1}, local, ctx)
    assert result.content.startswith('2\ttwo\n') and result.metadata['bounded_read']

    legacy = monitored(Legacy())
    for _ in range(CONSECUTIVE_FAILURE_LIMIT + 1):
        result = await tool.execute({'path': 'a', 'bounded': True}, legacy, ctx)
        assert result.is_error and 'not supported' in result.content
    assert legacy.is_alive and not (await tool.execute({'path': 'a'}, legacy, ctx)).is_error

    with pytest.raises(EnvironmentUnavailableError):
        local._die('container_gone', 'test')
    with pytest.raises(EnvironmentUnavailableError):
        await local.read_text_page('a')


@pytest.mark.asyncio
async def test_cancellation_settles_reader(tmp_path, monkeypatch):
    import asyncio
    import threading

    import garuda.workspace.text_reads as reads

    started, settled = threading.Event(), threading.Event()

    def slow_reader(path, offset, limit, tail, cancelled, digest=False):
        started.set()
        assert cancelled.wait(3)
        settled.set()
        raise InterruptedError('cancelled')

    monkeypatch.setattr(reads, 'read_local_page', slow_reader)
    task = asyncio.create_task(LocalEnvironment(tmp_path).read_text_page('a'))
    assert await asyncio.to_thread(started.wait, 3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert settled.is_set()


@pytest.mark.asyncio
async def test_path_policy_and_symlinks_are_preserved(tmp_path):
    from garuda.workspace.protocol import WorkspaceAccessDenied
    from garuda.workspace.sandbox import SandboxEnvironment
    from garuda.workspace.sandbox_policy import SandboxPolicy

    root = tmp_path / 'workspace'
    root.mkdir()
    (root / 'inside').write_text('allowed\n')
    (tmp_path / 'outside').write_text('outside\n')
    (root / 'alias').symlink_to(root / 'inside')
    (root / 'escape').symlink_to(tmp_path / 'outside')
    env = LocalEnvironment(root)
    assert (await env.read_text_page('alias')).lines == ('allowed',)
    with pytest.raises(WorkspaceAccessDenied):
        await env.read_text_page('../outside')
    sandbox = SandboxEnvironment(root, policy=SandboxPolicy(require_sandbox=False))
    with pytest.raises(WorkspaceAccessDenied):
        await sandbox.read_text_page('escape')
    assert (await sandbox.read_text_page('alias')).lines == ('allowed',)


@pytest.mark.asyncio
@pytest.mark.parametrize('backend', ['docker', 'remote'])
async def test_container_adapters_explicitly_refuse_bounded_io(backend):
    from garuda.workspace.docker import DockerEnvironment
    from garuda.workspace.remote import RemoteEnvironment

    cls = DockerEnvironment if backend == 'docker' else RemoteEnvironment
    env = object.__new__(cls)  # unsupported capability must not launch any transport
    with pytest.raises(NotImplementedError):
        await env.read_text_page('anything')


@pytest.mark.asyncio
async def test_utf8_and_crlf_across_chunk_boundaries(tmp_path):
    from garuda.workspace.text_reads import CHUNK_BYTES

    text = 'x' * (CHUNK_BYTES - 1) + 'α\r\nβ\r\n'
    (tmp_path / 'a').write_bytes(text.encode())
    page = await LocalEnvironment(tmp_path).read_text_page('a', tail=True)
    assert page.total_lines == 2
    assert page.lines[0] == 'x' * 2001
    assert page.lines[1] == 'β'
