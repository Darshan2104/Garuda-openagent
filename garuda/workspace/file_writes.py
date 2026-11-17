"""UTF-8 local writes with atomic publication when directory permissions allow."""

import errno
import os
import stat
import uuid
from pathlib import Path


def write_local_text(target: Path, content: str) -> None:
    # Validate before creating directories or opening any file. Bytes also avoid
    # platform newline translation, preserving CRLF, bare CR and mixed endings.
    payload = content.encode("utf-8")
    # Keep local writes' existing symlink-following semantics: replace the
    # referent, not the workspace's symlink. Path policy belongs to the caller.
    try:
        target = target.resolve()
    except RuntimeError as exc:
        # Python 3.12 reports resolution loops as RuntimeError rather than the
        # OS error that file tools handle. Newer Python versions raise OSError.
        raise OSError(errno.ELOOP, "Symlink loop", str(target)) from exc
    target.parent.mkdir(parents=True, exist_ok=True)
    mode = None
    try:
        info = target.stat()
    except FileNotFoundError:
        pass
    else:
        if not stat.S_ISREG(info.st_mode):
            raise OSError(f"Cannot replace non-regular file: {target}")
        # Rename only requires directory permission; retain the old requirement
        # that the destination itself must be writable, without truncating it.
        fd = os.open(target, os.O_WRONLY | os.O_NONBLOCK)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise OSError(f"Cannot replace non-regular file: {target}")
            mode = stat.S_IMODE(info.st_mode)
        finally:
            os.close(fd)

    staging = target.parent / f".garuda-write-{uuid.uuid4().hex}.tmp"
    # Exclusive creation cannot follow a preexisting symlink. New files retain
    # normal 0666 & umask creation semantics; existing files stage privately.
    try:
        fd = os.open(staging, os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                     0o600 if mode is not None else 0o666)
    except OSError as exc:
        if mode is None or exc.errno not in (errno.EACCES, errno.EPERM, errno.EROFS):
            raise
        # Preserve writes to an existing writable file when its directory does
        # not allow staging. Open without creation/truncation and revalidate the
        # actual descriptor before truncating. Encoding has already succeeded;
        # this compatibility path cannot protect against mid-write I/O failure.
        fd = os.open(target, os.O_WRONLY | os.O_NONBLOCK)
        with os.fdopen(fd, "wb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise OSError(f"Cannot write non-regular file: {target}") from exc
            stream.truncate(0)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        return
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            if mode is not None:
                os.fchmod(stream.fileno(), mode)
            os.fsync(stream.fileno())
        os.replace(staging, target)
    finally:
        staging.unlink(missing_ok=True)
