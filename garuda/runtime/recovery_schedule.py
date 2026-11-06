"""User-cron and opt-in macOS LaunchAgent scheduling for quota recovery checks.

The schedule lives outside any workspace.  A small owner-only wrapper supplies
absolute executable paths, while a marker-delimited crontab block lets Garuda
update its own entry without owning the rest of the user's crontab.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import plistlib
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


def _write_private_text(
    directory_fd: int, root: Path, path: Path, content: str, *, mode: int = 0o700,
) -> None:
    """Atomically publish an owner-only file through the retained directory."""
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
            mode,
            dir_fd=directory_fd,
        )
        remaining = memoryview(content.encode("utf-8"))
        while remaining:
            written = os.write(fd, remaining)
            if written <= 0:
                raise OSError("wrapper write made no progress")
            remaining = remaining[written:]
        os.fchmod(fd, mode)
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
    else:
        reason = _wrapper_reason(metadata, root)
    return {**metadata, "installed": reason is None, "job_count": len(blocks), "reason": reason}


def _wrapper_reason(metadata: dict, root: Path) -> str | None:
    _, script_path, _ = _paths(root)
    if metadata.get("script_path") != str(script_path):
        return "stored wrapper path does not match this recovery root"
    try:
        info = script_path.stat(follow_symlinks=False)
    except OSError as exc:
        return f"recovery wrapper is unavailable: {exc.strerror}"
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) not in (0o500, 0o700)):
        return "recovery wrapper must be an owner-only executable regular file"
    return None


def install(root: Path, *, backend: str = "cron") -> dict:
    """Install hourly quota recovery; cron remains the default backend."""
    if backend not in {"cron", "launchd"}:
        raise ValueError("unknown recovery scheduler backend")
    if backend == "launchd":
        return _install_launchd(root)
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
        _check_backend_change(root, "cron")
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
    """Disable recovery before removing only Garuda's selected schedule."""
    root = _absolute(Path(root))
    settings_path, _, _ = _paths(root)

    with ss.exclusive_lock(root) as directory_fd:
        existing = ss.read_document(settings_path, versions=(1,)) or {}
        if existing.get("backend", "cron") not in {"cron", "launchd"}:
            raise ValueError("unknown stored recovery scheduler backend")
        if existing.get("backend") == "launchd":
            disabled = {**existing, "enabled": False}
            ss.write_locked_document(directory_fd, settings_path, disabled)
            return _disable_launchd(disabled, root)
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
    """Report persisted enablement and installation without executing a tick."""
    root = _absolute(Path(root))
    settings_path, _, _ = _paths(root)
    with ss.exclusive_lock(root):
        metadata = ss.read_document(settings_path, versions=(1,))
        if metadata is None:
            metadata = _metadata(root, enabled=False, codex_executable=None)
        if metadata.get("backend", "cron") not in {"cron", "launchd"}:
            raise ValueError("unknown stored recovery scheduler backend")
        if metadata.get("backend") == "launchd":
            try:
                return _launchd_status(metadata, root)
            except (OSError, RuntimeError, subprocess.SubprocessError, ss.StorageError):
                return {**metadata, "installed": False, "job_count": None,
                        "reason": "launchd installation could not be observed"}
        current = _read_crontab()
        _, blocks = _split_crontab(current)
        return _status(metadata, root, blocks)


def _launchd_paths(root: Path) -> tuple[str, Path, str]:
    suffix = hashlib.sha256(os.fsencode(root)).hexdigest()[:16]
    label = f"com.garuda.quota-recovery.{suffix}"
    path = _absolute(Path.home()) / "Library" / "LaunchAgents" / f"{label}.plist"
    return label, path, f"gui/{os.getuid()}/{label}"


def _plist(root: Path) -> str:
    label, _, _ = _launchd_paths(root)
    return plistlib.dumps({
        "Label": label,
        "ProgramArguments": [str(root / _SCRIPT)],
        "StartCalendarInterval": {"Minute": 27},
        "WorkingDirectory": str(root),
    }).decode("utf-8")


def _require_launchd() -> None:
    if sys.platform != "darwin":
        raise RuntimeError("the launchd recovery backend requires macOS; recovery remains disabled")


@contextlib.contextmanager
def _launch_agents(*, create: bool = False):
    """Retain no-follow descriptors without changing shared directory permissions."""
    with contextlib.ExitStack() as stack:
        bindings = []
        directory = _absolute(Path.home())
        fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        stack.callback(os.close, fd)
        for name in (None, "Library", "LaunchAgents"):
            if name is not None:
                directory /= name
                if create:
                    try:
                        os.mkdir(name, 0o700, dir_fd=fd)
                    except FileExistsError:
                        pass
                fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                stack.callback(os.close, fd)
            info = os.fstat(fd)
            if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o022:
                raise ss.StorageUnavailable("LaunchAgents directories must be owned by this user "
                                            "and not writable by other users")
            bindings.append((directory, info))
        yield fd
        for directory, info in bindings:
            current = directory.stat(follow_symlinks=False)
            if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
                raise ss.StorageUnavailable("LaunchAgents directory changed; refusing")


def _read_plist(directory_fd: int, root: Path) -> None:
    _, path, _ = _launchd_paths(root)
    expected = _plist(root).encode("utf-8")
    with os.fdopen(os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                           dir_fd=directory_fd), "rb") as handle:
        info = os.fstat(handle.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600):
            raise RuntimeError("recovery plist must be an owner-only regular file (0600)")
        if handle.read(len(expected) + 1) != expected:
            raise RuntimeError("recovery plist does not match this root's exact job definition")


def _launchctl(*arguments: str) -> subprocess.CompletedProcess:
    return subprocess.run(["/bin/launchctl", *arguments], capture_output=True, text=True,
                          check=False, timeout=15)


def _read_launchd(root: Path) -> str | None:
    _require_launchd()
    label, _, target = _launchd_paths(root)
    result = _launchctl("print", target)
    if result.returncode == 0:
        return result.stdout
    # Only the exact missing-service diagnostic proves absence. Permission,
    # missing GUI domain, and unknown failures must never become zero jobs.
    if result.returncode == 113 and re.fullmatch(
        rf'(?:Bad request\.\s*)?Could not find service "{re.escape(label)}" '
        rf'in domain for user gui: {os.getuid()}', result.stderr.strip(),
    ):
        return None
    result.check_returncode()
    raise AssertionError("unreachable")


def _loaded_matches(output: str, root: Path) -> bool:
    _, path, target = _launchd_paths(root)
    # launchctl print is diagnostic text, not a stable API. Unrecognized output
    # fails closed rather than treating a successful exit as proof of identity.
    return (
        output.startswith(f"{target} = {{\n")
        and re.findall(r"^\tpath = (.*)$", output, re.M) == [str(path)]
        and re.findall(r"^\tprogram = (.*)$", output, re.M) == [str(root / _SCRIPT)]
        and re.findall(r"^\targuments = \{\n(.*?)^\t\}", output, re.M | re.S)
        == [f"\t\t{root / _SCRIPT}\n"]
        and re.findall(r"^\tstate = ([^\r\n]+)$", output, re.M)
        in (["running"], ["not running"])
    )


def _launchd_status(metadata: dict, root: Path) -> dict:
    observed = _read_launchd(root)
    reason = None
    label, path, _ = _launchd_paths(root)
    if observed is None:
        reason = "managed LaunchAgent is not loaded"
    elif not _loaded_matches(observed, root):
        reason = "loaded LaunchAgent does not match this recovery root's identity and wrapper"
    elif metadata.get("label") != label or metadata.get("plist_path") != str(path):
        reason = "stored LaunchAgent identity does not match this recovery root"
    else:
        reason = _wrapper_reason(metadata, root)
        if reason is None:
            with _launch_agents() as fd:
                _read_plist(fd, root)
            with os.fdopen(os.open(root / _SCRIPT, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK),
                           "rb") as handle:
                info = os.fstat(handle.fileno())
                if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                        or stat.S_IMODE(info.st_mode) not in (0o500, 0o700)):
                    raise RuntimeError("recovery wrapper must be an owner-only executable regular file")
                raw = handle.read(65537)
            if hashlib.sha256(raw).hexdigest() != metadata.get("wrapper_sha256"):
                reason = "recovery wrapper does not match its installed interpreter and environment"
    return {**metadata, "installed": reason is None,
            "job_count": int(observed is not None), "reason": reason}


def _check_backend_change(root: Path, backend: str) -> None:
    existing = ss.read_document(root / _SETTINGS, versions=(1,)) or {}
    prior = existing.get("backend", "cron")
    if not existing or prior == backend:
        return
    if prior not in {"cron", "launchd"}:
        raise ValueError("unknown stored recovery scheduler backend")
    if prior == "launchd":
        remaining = _read_launchd(root) is not None
        _, path, _ = _launchd_paths(root)
        remaining = remaining or os.path.lexists(path)
    else:
        _, blocks = _split_crontab(_read_crontab())
        remaining = bool(blocks)
    if existing.get("enabled") is not False or remaining:
        raise RuntimeError("disable the previous recovery backend and verify removal before switching")


def _install_launchd(root: Path) -> dict:
    root = _absolute(Path(root))
    settings_path, script_path, log_path = _paths(root)
    with ss.exclusive_lock(root) as directory_fd:
        _check_backend_change(root, "launchd")
        existing = ss.read_document(settings_path, versions=(1,)) or {}
        ss.write_locked_document(directory_fd, settings_path, {**existing, "version": 1,
                                                             "enabled": False, "backend": "launchd"})
        _require_launchd()
        codex = _resolved_executable("codex", required=True)
        assert codex is not None
        python = str(_absolute(Path(sys.executable)))
        wrapper = _wrapper(python, codex, root, log_path)
        label, path, target = _launchd_paths(root)
        disabled = {**_metadata(root, enabled=False, codex_executable=codex),
                    "backend": "launchd", "label": label, "plist_path": str(path),
                    "python_executable": python,
                    "wrapper_sha256": hashlib.sha256(wrapper.encode("utf-8")).hexdigest()}
        ss.write_locked_document(directory_fd, settings_path, disabled)
        observed = _read_launchd(root)
        if observed is not None:
            if not _loaded_matches(observed, root):
                raise RuntimeError("refusing to replace a mismatched loaded LaunchAgent")
            _launchctl("bootout", target).check_returncode()
            if _read_launchd(root) is not None:
                raise RuntimeError("LaunchAgent unload could not be verified; recovery remains disabled")
        _write_private_text(directory_fd, root, script_path, wrapper)
        with _launch_agents(create=True) as fd:
            if os.path.lexists(path):
                _read_plist(fd, root)
            _write_private_text(fd, path.parent, path, _plist(root), mode=0o600)
            _read_plist(fd, root)
            _launchctl("bootstrap", target.rsplit("/", 1)[0], str(path)).check_returncode()
            status = _launchd_status(disabled, root)
            if not status["installed"]:
                raise RuntimeError(f"quota recovery installation could not be verified: {status['reason']}; "
                                   "recovery remains disabled")
        enabled = {**disabled, "enabled": True}
        ss.write_locked_document(directory_fd, settings_path, enabled)
        return {**status, **enabled}


def _disable_launchd(metadata: dict, root: Path) -> dict:
    observed = _read_launchd(root)
    _, path, target = _launchd_paths(root)
    if observed is not None:
        if not _loaded_matches(observed, root):
            raise RuntimeError("refusing to unload a mismatched LaunchAgent; recovery remains disabled")
        _launchctl("bootout", target).check_returncode()
    if _read_launchd(root) is not None:
        raise RuntimeError("LaunchAgent removal could not be verified; recovery remains disabled")
    if os.path.lexists(path):
        with _launch_agents() as fd:
            _read_plist(fd, root)
            os.unlink(path.name, dir_fd=fd)
            os.fsync(fd)
        if os.path.lexists(path):
            raise RuntimeError("LaunchAgent plist removal could not be verified; recovery remains disabled")
    return {**metadata, "installed": False, "job_count": 0,
            "reason": "managed LaunchAgent is not loaded"}
