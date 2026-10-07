"""Opaque project identity and per-project session names (plan task B.1, #157).

A **project id** is a versioned, domain-separated HMAC-SHA256 of a project's
canonical identity — its repository root (linked worktrees and symlinked paths
resolve to the same root, see :func:`garuda.core.sessions.project_root`) or, for
non-Git work, its real path. The HMAC key is random, user-local, created
atomically with mode ``0600`` beside the session store, and never exported, so
an id names a project without revealing its path and cannot be computed by
anyone without the key.

If the key is missing while sessions already carry project ids, nothing
generates a replacement silently — that would split every project into two.
Allocation refuses with ``session.project_key_missing`` until the identities are
recovered.

Session **names** are unique within a project. A name is reserved by creating
``<store>/.names/<project id>/<name>`` with an exclusive ``mkdir``, so two
processes can never receive the same name and suffix selection cannot race.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import stat
from dataclasses import dataclass
from pathlib import Path

PROJECT_ID_PREFIX = "p1_"
_DOMAIN = b"garuda/project-id/v1\0"
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,47}$")
MAX_NAME_SUFFIX = 999


class ProjectIdentityError(Exception):
    """Base error; ``code`` is a stable diagnostic."""

    code = "session.project_identity"


class ProjectKeyMissing(ProjectIdentityError):
    code = "session.project_key_missing"


class SessionNameTaken(ProjectIdentityError):
    code = "session.name_taken"


@dataclass(frozen=True)
class ProjectIdentity:
    project_id: str
    path: str  # local metadata only: never exported in aggregates
    fs_id: str


def identity_dir(store_root: Path) -> Path:
    return Path(store_root) / ".identity"


def _key_path(store_root: Path) -> Path:
    return identity_dir(store_root) / "key"


def _any_identified_session(store_root: Path) -> bool:
    import json

    if not Path(store_root).is_dir():
        return False
    for directory in Path(store_root).iterdir():
        meta = directory / "meta.json"
        if not meta.is_file():
            continue
        try:
            if json.loads(meta.read_text(encoding="utf-8")).get("project_id"):
                return True
        except (OSError, ValueError):
            continue
    return False


def load_key(store_root: Path) -> bytes:
    """The store's HMAC key, created on first use; refuses if it was lost."""
    from garuda.runtime.strict_store import exclusive_lock

    path = _key_path(store_root)
    with exclusive_lock(identity_dir(store_root)):
        try:
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except FileNotFoundError:
            fd = None
        except OSError as exc:
            raise ProjectIdentityError(f"cannot read the project key at {path}: {exc}") from exc
        if fd is not None:
            with os.fdopen(fd, "r", encoding="ascii") as handle:
                text = handle.read().strip()
            if not re.fullmatch(r"[0-9a-f]{64}", text):
                raise ProjectIdentityError(f"the project key at {path} is malformed; refusing")
            return bytes.fromhex(text)
        if _any_identified_session(store_root):
            raise ProjectKeyMissing(
                f"{ProjectKeyMissing.code}: the project key at {path} is missing but saved "
                "sessions already carry project ids; a new key would split every project. "
                "Run `garuda doctor --recover-project-ids` to recover them."
            )
        key = secrets.token_hex(32)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags, 0o600)
        try:
            os.write(fd, key.encode("ascii"))
            os.fsync(fd)
        finally:
            os.close(fd)
        return bytes.fromhex(key)


def compute_project_id(key: bytes, canonical: str) -> str:
    digest = hmac.new(key, _DOMAIN + canonical.encode("utf-8"), hashlib.sha256).hexdigest()
    return PROJECT_ID_PREFIX + digest[:32]


def existing_project_id(store_root: Path, workspace: str | Path) -> str:
    """Resolve against an existing key without allocation, locks or Git probes.

    Used for read-only previews. Missing/corrupt identity evidence refuses;
    preview cannot repair it or create a replacement identity.
    """
    from garuda.core.sessions import project_root

    path = _key_path(store_root)
    try:
        parent = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0))
        try:
            fd = os.open(path.name, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0),
                         dir_fd=parent)
        finally:
            os.close(parent)
    except FileNotFoundError as exc:
        raise ProjectKeyMissing("project identity key unavailable; preview cannot allocate or recover it") from exc
    except OSError as exc:
        raise ProjectIdentityError("cannot read existing project identity key") from exc
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ProjectIdentityError("project identity key is not a regular file")
        data = os.read(fd, 129)
    finally:
        os.close(fd)
    try:
        text = data.decode("ascii").strip()
    except UnicodeDecodeError as exc:
        raise ProjectIdentityError("project identity key is malformed") from exc
    if len(data) > 128 or not re.fullmatch(r"[0-9a-f]{64}", text):
        raise ProjectIdentityError("project identity key is malformed")
    canonical = project_root(os.path.abspath(os.path.expanduser(str(workspace))))
    return compute_project_id(bytes.fromhex(text), canonical)


def project_identity(store_root: Path, workspace: str | Path) -> ProjectIdentity:
    """The project a workspace belongs to, as an opaque id plus local metadata."""
    from garuda.core.sessions import project_root

    canonical = project_root(os.path.abspath(os.path.expanduser(str(workspace))))
    return ProjectIdentity(
        compute_project_id(load_key(store_root), canonical), canonical, filesystem_identity(canonical)
    )


def filesystem_identity(path: str) -> str:
    """What recovery checks a recorded project path against: device and inode,
    plus the repository's root commit when it has one.

    An inode alone is not enough: a filesystem may hand a deleted directory's
    inode to the next one made at the same path, so a replaced repository
    would pass. The root commit tells two histories apart. A repository with
    no commits yet is identified by device and inode only. ``""`` when the
    path is gone.
    """
    try:
        info = os.stat(path)
    except OSError:
        return ""
    identity = f"{info.st_dev}:{info.st_ino}"
    try:
        import subprocess

        roots = subprocess.run(
            ["git", "-C", path, "rev-list", "--max-parents=0", "HEAD"],
            capture_output=True, text=True, timeout=10,
            env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "GIT_CONFIG_NOSYSTEM": "1",
                 "GIT_CONFIG_GLOBAL": os.devnull, "HOME": os.environ.get("HOME", "/")},
        )
    except (OSError, subprocess.SubprocessError):
        return identity
    if roots.returncode == 0 and roots.stdout.split():
        identity += ":" + sorted(roots.stdout.split())[0][:16]
    return identity


def slugify(text: str, default: str = "session") -> str:
    """A short, readable name from a task: lowercase words joined by '-'."""
    words = re.findall(r"[a-z0-9]+", (text or "").lower())
    slug = ""
    for word in words:
        candidate = f"{slug}-{word}" if slug else word
        if len(candidate) > 40:
            break
        slug = candidate
        if slug.count("-") >= 4:
            break
    return slug or default


def validate_name(name: str) -> str:
    if not _NAME_RE.match(name or ""):
        raise ProjectIdentityError(
            f"session name {name!r} must be lowercase letters, digits and '-', "
            "starting with a letter or digit, at most 48 characters"
        )
    return name


def _names_dir(store_root: Path, project_id: str) -> Path:
    return Path(store_root) / ".names" / project_id


def allocate_name(
    store_root: Path, project_id: str, session_id: str, *, wanted: str | None, task: str
) -> str:
    """Reserve a unique name in the project.

    An explicit ``wanted`` name must be free; otherwise the task's slug is used
    with the first free ``-2``, ``-3`` … suffix.
    """
    base = Path(store_root) / ".names"
    base.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory = _names_dir(store_root, project_id)
    directory.mkdir(mode=0o700, exist_ok=True)
    if wanted is not None:
        candidates = [validate_name(wanted)]
    else:
        stem = slugify(task)
        candidates = [stem] + [f"{stem}-{n}" for n in range(2, MAX_NAME_SUFFIX + 1)]
    for candidate in candidates:
        try:
            os.mkdir(directory / candidate, 0o700)
        except FileExistsError:
            continue
        (directory / candidate / "session").write_text(session_id, encoding="utf-8")
        return candidate
    if wanted is not None:
        raise SessionNameTaken(
            f"{SessionNameTaken.code}: a session named {wanted!r} already exists in this project"
        )
    raise ProjectIdentityError(f"no free name for {candidates[0]!r} in this project")


def session_for_name(store_root: Path, project_id: str, name: str) -> str | None:
    marker = _names_dir(store_root, project_id) / name / "session"
    try:
        return marker.read_text(encoding="utf-8").strip() or None
    except (OSError, ValueError):
        return None
