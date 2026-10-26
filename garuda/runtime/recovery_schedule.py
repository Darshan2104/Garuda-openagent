"""User-cron scheduling for quota recovery checks.

The schedule lives outside any workspace.  A small owner-only wrapper supplies
absolute executable paths, while a marker-delimited crontab block lets Garuda
update its own entry without owning the rest of the user's crontab.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import uuid
from pathlib import Path

from garuda.runtime import strict_store as ss

_BEGIN = "# BEGIN garuda-quota-recovery"
_END = "# END garuda-quota-recovery"
_SCHEDULE = "27 * * * *"
_SETTINGS = "settings.json"
_SCRIPT = "quota-recovery.sh"
_LOG = "quota-recovery.log"
_PRESERVED_ENV = ("PATH", "HOME", "USER", "LANG")
_AUTHORITY_PATH_ENV = (
    "GARUDA_GLOBAL_SETTINGS",
    "GARUDA_SESSIONS_DIR",
    "GARUDA_LEASES_DIR",
    "GARUDA_RECOVERY_ROOT",
)


def _absolute(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _resolved_executable(name: str, *, required: bool) -> str | None:
    found = shutil.which(name)
    if found is None:
        if required:
            raise FileNotFoundError(f"cannot install quota recovery: {name!r} is not on PATH")
        return None
    return str(Path(found).resolve())


def _paths(root: Path) -> tuple[Path, Path, Path]:
    return root / _SETTINGS, root / _SCRIPT, root / _LOG


def _write_private_text(directory_fd: int, root: Path, path: Path, content: str) -> None:
    """Atomically publish an owner-only executable through the locked directory."""
    temporary = f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    fd: int | None = None
    try:
        bound = os.fstat(directory_fd)
        current = root.stat(follow_symlinks=False)
        if (current.st_dev, current.st_ino) != (bound.st_dev, bound.st_ino):
            raise ss.StorageUnavailable("schedule directory changed before publication; refusing")
        fd = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o700,
            dir_fd=directory_fd,
        )
        remaining = memoryview(content.encode("utf-8"))
        while remaining:
            written = os.write(fd, remaining)
            if written <= 0:
                raise OSError("wrapper write made no progress")
            remaining = remaining[written:]
        os.fchmod(fd, 0o700)
        os.fsync(fd)
        os.close(fd)
        fd = None
        os.replace(temporary, path.name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
        os.fsync(directory_fd)
    except OSError as exc:
        raise ss.StorageUnavailable(f"cannot publish recovery wrapper {path}: {exc}") from exc
    finally:
        if fd is not None:
            os.close(fd)
        try:
            os.unlink(temporary, dir_fd=directory_fd)
        except FileNotFoundError:
            pass


def _wrapper(python: str, codex: str, root: Path, log_path: Path) -> str:
    codex_directory = str(Path(codex).parent)
    preserved: dict[str, str] = {}
    for name in _PRESERVED_ENV:
        value = os.environ.get(name)
        if value:
            preserved[name] = value
    preserved["PATH"] = f"{codex_directory}{os.pathsep}{preserved.get('PATH', '/usr/bin:/bin')}"
    preserved.setdefault("LANG", "C.UTF-8")
    for name in _AUTHORITY_PATH_ENV:
        value = os.environ.get(name)
        if value:
            preserved[name] = str(_absolute(Path(value).expanduser()))

    assignments = [f"{name}={shlex.quote(value)}" for name, value in preserved.items()]
    exports = " ".join(preserved)
    return "".join([
        "#!/bin/sh\n",
        "umask 077\n",
        *(f"{assignment}\n" for assignment in assignments),
        f"export {exports}\n",
        f"exec {shlex.quote(python)} -m garuda.interfaces.main recovery tick --root {shlex.quote(str(root))} ",
        f">> {shlex.quote(str(log_path))} 2>&1\n",
    ])


def _cron_quote(path: Path) -> str:
    value = str(path)
    if "\n" in value or "\r" in value:
        raise ValueError("the recovery schedule path may not contain a newline")
    # Cron interprets percent signs before passing the command to the shell,
    # including percent signs inside shell quotes.
    return shlex.quote(value).replace("%", r"\%")


def _block(script_path: Path) -> str:
    return f"{_BEGIN}\n{_SCHEDULE} {_cron_quote(script_path)}\n{_END}\n"


def _split_crontab(crontab: str) -> tuple[str, list[list[str]]]:
    lines = crontab.splitlines(keepends=True)
    kept: list[str] = []
    inside = False
    blocks: list[list[str]] = []
    for line in lines:
        marker = line.rstrip("\r\n")
        if marker == _BEGIN:
            if inside:
                raise RuntimeError("nested garuda quota-recovery crontab block")
            inside = True
            blocks.append([])
            continue
        if marker == _END:
            if not inside:
                raise RuntimeError("garuda quota-recovery crontab end marker has no beginning")
            inside = False
            continue
        if inside:
            if line.strip():
                blocks[-1].append(line.strip())
        else:
            kept.append(line)
    if inside:
        raise RuntimeError("garuda quota-recovery crontab block has no end marker")
    return "".join(kept), blocks


def _read_crontab() -> str:
    result = subprocess.run(
        ["crontab", "-l"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 0:
        return result.stdout
    message = result.stderr.strip()
    if result.returncode == 1 and re.fullmatch(
        r"(?:crontab:\s*)?no crontab for [^\r\n]+", message, flags=re.IGNORECASE
    ):
        return ""
    result.check_returncode()
    raise AssertionError("unreachable")


def _write_crontab(content: str) -> None:
    subprocess.run(
        ["crontab", "-"],
        input=content,
        capture_output=True,
        text=True,
        check=True,
    )


def _metadata(
    root: Path,
    *,
    enabled: bool,
    codex_executable: str | None,
) -> dict:
    _, script_path, log_path = _paths(root)
    return {
        "version": 1,
        "enabled": enabled,
        "codex_executable": codex_executable,
        "schedule": _SCHEDULE,
        "script_path": str(script_path),
        "log_path": str(log_path),
    }


def _status(metadata: dict, root: Path, blocks: list[list[str]]) -> dict:
    """Readiness is observed locally, separately from persisted enablement."""
    _, script_path, _ = _paths(root)
    reason = None
    if not blocks:
        reason = "managed cron entry is missing"
    elif len(blocks) != 1:
        reason = "multiple managed cron entries are present; run garuda recovery enable to repair"
    elif blocks[0] != [f"{_SCHEDULE} {_cron_quote(script_path)}"]:
        reason = "managed cron entry does not match the schedule and wrapper for this recovery root"
    elif metadata.get("script_path") != str(script_path):
        reason = "stored wrapper path does not match this recovery root"
    else:
        try:
            info = script_path.stat(follow_symlinks=False)
        except OSError as exc:
            reason = f"recovery wrapper is unavailable: {exc.strerror}"
        else:
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) not in (0o500, 0o700)):
                reason = "recovery wrapper must be an owner-only executable regular file"
    return {**metadata, "installed": reason is None, "job_count": len(blocks), "reason": reason}


def install(root: Path) -> dict:
    """Install or refresh the hourly quota-recovery cron entry."""
    root = _absolute(Path(root))
    settings_path, script_path, log_path = _paths(root)
    codex = _resolved_executable("codex", required=True)
    assert codex is not None
    # Keep a virtualenv's interpreter path: resolving its symlink can select
    # the base interpreter, which does not have this Garuda installation.
    python = str(_absolute(Path(sys.executable)))
    wrapper = _wrapper(python, codex, root, log_path)
    block = _block(script_path)

    with ss.exclusive_lock(root) as directory_fd:
        current = _read_crontab()
        unrelated, _ = _split_crontab(current)
        if unrelated and not unrelated.endswith(("\n", "\r")):
            unrelated += "\n"
        desired = unrelated + block

        _write_private_text(directory_fd, root, script_path, wrapper)
        disabled = _metadata(root, enabled=False, codex_executable=codex)
        ss.write_locked_document(directory_fd, settings_path, disabled)
        if desired != current:
            _write_crontab(desired)
        _, observed = _split_crontab(_read_crontab())
        status = _status(disabled, root, observed)
        if not status["installed"]:
            raise RuntimeError(f"quota recovery installation could not be verified: {status['reason']}; "
                               "recovery remains disabled")
        enabled = _metadata(root, enabled=True, codex_executable=codex)
        ss.write_locked_document(directory_fd, settings_path, enabled)
        return {**status, **enabled}


def disable(root: Path) -> dict:
    """Disable recovery before removing only Garuda's exact managed block."""
    root = _absolute(Path(root))
    settings_path, _, _ = _paths(root)

    with ss.exclusive_lock(root) as directory_fd:
        existing = ss.read_document(settings_path, versions=(1,)) or {}
        codex = _resolved_executable("codex", required=False)
        if codex is None:
            prior = existing.get("codex_executable")
            codex = prior if isinstance(prior, str) and Path(prior).is_absolute() else None
        disabled = _metadata(root, enabled=False, codex_executable=codex)
        ss.write_locked_document(directory_fd, settings_path, disabled)

        current = _read_crontab()
        desired, _ = _split_crontab(current)
        if desired != current:
            _write_crontab(desired)
        _, observed = _split_crontab(_read_crontab())
        if observed:
            raise RuntimeError("quota recovery cron removal could not be verified; recovery remains disabled")
        return _status(disabled, root, observed)


def schedule_status(root: Path) -> dict:
    """Report persisted enablement and the current managed cron entry."""
    root = _absolute(Path(root))
    settings_path, _, _ = _paths(root)
    with ss.exclusive_lock(root):
        metadata = ss.read_document(settings_path, versions=(1,))
        if metadata is None:
            metadata = _metadata(root, enabled=False, codex_executable=None)
        current = _read_crontab()
        _, blocks = _split_crontab(current)
        return _status(metadata, root, blocks)
