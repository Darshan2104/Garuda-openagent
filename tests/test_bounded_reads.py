
import pytest

from garuda.tools.files import ReadFileTool
from garuda.tools.protocol import ToolContext
from garuda.workspace.local import LocalEnvironment


@pytest.mark.asyncio
async def test_page_does_not_use_whole_file_read(tmp_path, monkeypatch):
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
async def test_long_lines_have_bounded_allocation(tmp_path):
    import tracemalloc

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

    from garuda.workspace.text_reads import CHUNK_BYTES

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
async def test_scan_ceiling_and_missing_file(tmp_path, monkeypatch):
    import garuda.workspace.text_reads as reads

    monkeypatch.setattr(reads, 'MAX_SCAN_BYTES', 8192)
    (tmp_path / 'large').write_bytes(b'x' * 20000)
    env = LocalEnvironment(tmp_path)
    with pytest.raises(OSError, match='scan budget'):
        await env.read_text_page('large', tail=True)
    with pytest.raises(FileNotFoundError):
        await env.read_text_page('missing')
    with pytest.raises(ValueError):
        await env.read_text_page('large', offset=2, tail=True)


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
async def test_cancellation_settles_reader(tmp_path, monkeypatch):
    import asyncio
    import threading

    import garuda.workspace.text_reads as reads

    started, settled = threading.Event(), threading.Event()

    def slow_reader(path, offset, limit, tail, cancelled):
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
