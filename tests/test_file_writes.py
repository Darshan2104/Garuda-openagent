"""File mutation must preserve bytes on failure and round-trip local newlines."""

import errno
import json
import os
import stat
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from garuda.tools.edit import EditTool
from garuda.tools.files import WriteFileTool
from garuda.tools.multi_edit import MultiEditTool
from garuda.tools.protocol import ToolContext
from garuda.workspace.docker import DockerEnvironment
from garuda.workspace.local import LocalEnvironment
from garuda.workspace.remote import RemoteEnvironment

CTX = ToolContext(session_id="file-writes", post_edit_diagnostics=False)


@pytest.mark.parametrize("tool", [WriteFileTool(), EditTool(), MultiEditTool()], ids=lambda t: t.name)
@pytest.mark.parametrize("failure", ["surrogate", "disk-full"])
async def test_failed_write_is_a_tool_error_without_changing_file(tmp_path, monkeypatch, tool, failure):
    target = tmp_path / "keep.txt"
    original = b"keep me\n" * 100
    target.write_bytes(original)
    # A JSON string can decode to a lone surrogate, as real model arguments do.
    replacement = json.loads('"x\\ud800\\n"') if failure == "surrogate" else "replacement\n"
    if failure == "disk-full":
        def full_disk(fd):
            raise OSError(errno.ENOSPC, "No space left on device")

        monkeypatch.setattr("os.fsync", full_disk)
    change = {"old_string": "keep me\n", "new_string": replacement, "replace_all": True}
    args = {"path": target.name}
    if tool.name == "write_file":
        args["content"] = replacement
    elif tool.name == "edit":
        args.update(change)
    else:
        args["edits"] = [change]
    try:
        result = await tool.execute(args, LocalEnvironment(tmp_path), CTX)
    finally:
        assert target.read_bytes() == original
        assert list(tmp_path.iterdir()) == [target]
    assert result.is_error
    assert ("utf-8" if failure == "surrogate" else "No space left") in result.content


async def test_invalid_local_text_creates_no_files_or_directories(tmp_path):
    with pytest.raises(UnicodeEncodeError):
        await LocalEnvironment(tmp_path).write_file("new/invalid.txt", "\ud800")
    assert list(tmp_path.iterdir()) == []


async def test_write_symlink_loop_returns_tool_error(tmp_path):
    link = tmp_path / "loop.txt"
    link.symlink_to(link.name)
    result = await WriteFileTool().execute(
        {"path": link.name, "content": "replacement\n"}, LocalEnvironment(tmp_path), CTX,
    )
    assert result.is_error
    assert f"[Errno {errno.ELOOP}]" in result.content
    assert not result.metadata.get("permission_denied")
    assert link.is_symlink() and link.readlink() == Path(link.name)
    assert list(tmp_path.iterdir()) == [link]


@pytest.mark.skipif(os.geteuid() == 0, reason="root can write non-writable directories")
async def test_edit_writable_file_in_non_writable_directory(tmp_path):
    directory = tmp_path / "locked"
    directory.mkdir()
    target = directory / "keep.txt"
    target.write_bytes(b"keep me\r\n")
    target.chmod(0o644)
    directory.chmod(0o555)
    try:
        result = await EditTool().execute(
            {"path": "locked/keep.txt", "old_string": "keep me", "new_string": "changed"},
            LocalEnvironment(tmp_path), CTX,
        )
        assert not result.is_error, result.content
        assert target.read_bytes() == b"changed\r\n"
        assert stat.S_IMODE(target.stat().st_mode) == 0o644
        assert list(directory.iterdir()) == [target]
    finally:
        directory.chmod(0o755)


@pytest.mark.parametrize("error", [
    errno.EACCES, errno.EPERM, errno.EROFS, errno.ENOSPC, errno.EIO, errno.EEXIST,
])
async def test_staging_creation_fallback_is_limited_to_permission_errors(tmp_path, monkeypatch, error):
    target = tmp_path / "keep.txt"
    target.write_bytes(b"original\r\n")
    target.chmod(0o751)
    real_open = os.open

    def refuse_creation(path, flags, *args, **kwargs):
        if flags & os.O_CREAT:
            raise OSError(error, os.strerror(error))
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", refuse_creation)
    env = LocalEnvironment(tmp_path)
    if error in (errno.EACCES, errno.EPERM, errno.EROFS):
        await env.write_file(target.name, "replacement\r\n")
        assert target.read_bytes() == b"replacement\r\n"
    else:
        with pytest.raises(OSError) as raised:
            await env.write_file(target.name, "replacement\r\n")
        assert raised.value.errno == error
        assert target.read_bytes() == b"original\r\n"
    assert stat.S_IMODE(target.stat().st_mode) == 0o751
    assert list(tmp_path.iterdir()) == [target]


@pytest.mark.parametrize("operation", ["fsync", "replace"])
async def test_local_write_failure_keeps_target_and_removes_staging_file(tmp_path, monkeypatch, operation):
    target = tmp_path / "keep.txt"
    target.write_bytes(b"original\n")
    target.chmod(0o751)

    def fail(*args):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr("os." + operation, fail)
    with pytest.raises(OSError, match="No space left"):
        await LocalEnvironment(tmp_path).write_file(target.name, "replacement\n")
    assert target.read_bytes() == b"original\n"
    assert stat.S_IMODE(target.stat().st_mode) == 0o751
    assert list(tmp_path.iterdir()) == [target]


async def test_local_atomic_write_preserves_mode_and_follows_file_symlink(tmp_path):
    target = tmp_path / "real.txt"
    target.write_bytes(b"original\n")
    target.chmod(0o751)
    link = tmp_path / "link.txt"
    link.symlink_to(target.name)
    await LocalEnvironment(tmp_path).write_file(link.name, "replacement\n")
    assert link.is_symlink()
    assert target.read_bytes() == b"replacement\n"
    assert stat.S_IMODE(target.stat().st_mode) == 0o751
    assert set(tmp_path.iterdir()) == {target, link}


@pytest.mark.skipif(os.geteuid() == 0, reason="root can write read-only files")
async def test_local_write_refuses_read_only_destination(tmp_path):
    target = tmp_path / "real.txt"
    target.write_bytes(b"original\n")
    link = tmp_path / "link.txt"
    link.symlink_to(target.name)
    # Atomic rename must not bypass the destination's own write permissions.
    target.chmod(0o444)
    try:
        with pytest.raises(PermissionError):
            await LocalEnvironment(tmp_path).write_file(link.name, "must be refused\n")
        assert target.read_bytes() == b"original\n"
        assert link.is_symlink()
        assert set(tmp_path.iterdir()) == {target, link}
    finally:
        target.chmod(0o751)


@pytest.mark.parametrize("tool", [EditTool(), MultiEditTool()], ids=lambda t: t.name)
@pytest.mark.parametrize("original,old,new,expected", [
    (b"alpha\r\nbeta\r\ngamma\r\n", "beta", "BETA", b"alpha\r\nBETA\r\ngamma\r\n"),
    (b"alpha\rbeta\rgamma\r", "beta", "BETA", b"alpha\rBETA\rgamma\r"),
    (b"alpha\r\nbeta\ngamma\r", "beta", "BETA", b"alpha\r\nBETA\ngamma\r"),
    (b"alpha\r\nbeta\r\ngamma\r\n", "beta\ngamma", "BETA\nGAMMA",
     b"alpha\r\nBETA\r\nGAMMA\r\n"),
    (b"alpha\rbeta\rgamma\r", "beta\ngamma", "BETA\nGAMMA", b"alpha\rBETA\rGAMMA\r"),
], ids=["crlf", "cr", "mixed", "crlf-recovery", "cr-recovery"])
async def test_local_edit_preserves_line_endings(tmp_path, tool, original, old, new, expected):
    target = tmp_path / "endings.txt"
    target.write_bytes(original)
    change = {"old_string": old, "new_string": new}
    args = {"path": target.name}
    if tool.name == "edit":
        args.update(change)
    else:
        args["edits"] = [change]
    result = await tool.execute(args, LocalEnvironment(tmp_path), CTX)
    assert not result.is_error, result.content
    assert target.read_bytes() == expected


@pytest.mark.parametrize("backend", ["docker", "remote"])
@pytest.mark.parametrize("failure", ["encoding", "launch", "transfer", "exit", "success"])
async def test_container_upload_cleans_host_staging_file(tmp_path, monkeypatch, backend, failure):
    # Exercise host staging without requiring a running container.
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path))
    env = (DockerEnvironment("test") if backend == "docker"
           else RemoteEnvironment(["docker", "-H", "ssh://test"], "test"))
    process = AsyncMock()
    process.communicate.return_value = (b"", b"copy refused")
    process.returncode = 1 if failure == "exit" else 0
    if failure == "transfer":
        process.communicate.side_effect = OSError("transfer failed")

    async def launch_copy(*argv, **kwargs):
        # The upload must stage the exact payload before invoking the CLI.
        index = argv.index("cp")
        assert Path(argv[index + 1]).read_bytes() == b"payload\n"
        assert argv[index + 2] == "test:/workspace/keep.txt"
        assert list(argv[:index]) == (["docker"] if backend == "docker"
                                     else ["docker", "-H", "ssh://test"])
        if failure == "launch":
            raise OSError("launch failed")
        return process

    launch = AsyncMock(side_effect=launch_copy)
    monkeypatch.setattr("asyncio.create_subprocess_exec", launch)
    if failure == "success":
        await env.write_file("keep.txt", "payload\n")
    else:
        error = (UnicodeEncodeError if failure == "encoding" else
                 RuntimeError if failure == "exit" else OSError)
        with pytest.raises(error):
            await env.write_file("keep.txt", "\ud800" if failure == "encoding" else "payload\n")
    assert list(tmp_path.iterdir()) == []
    if failure == "encoding":
        launch.assert_not_awaited()
