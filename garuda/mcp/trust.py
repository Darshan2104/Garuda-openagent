"""Content-bound user trust for MCP servers that a project defines.

A project's ``.agent/mcp.json`` (or ``.garuda/``, or ``.cursor/mcp.json``) is
repository content. Starting one of its stdio servers runs a command the
repository chose, and connecting to one of its URLs sends requests the repository
chose. Neither may happen until the user trusts that exact entry for that exact
repository.

A grant binds:

- the repository identity (the canonical workspace path);
- the server name;
- a digest of the raw entry as written (before ``${VAR}`` interpolation, so no
  resolved secret is hashed or stored), plus directly identifiable repository
  launch files and their symlink targets. This includes attached option values,
  -r/--require/--import/--loader operands and project-local Python -m modules
  and script-containing directory trees. Trees exclude __pycache__, .pyc and
  .pyo. Discovery never imports repository code.

Changing the entry, a referenced script, or a symlink target invalidates the grant.
The store lives next to the user's global settings, is owner-only, is written
atomically under a lock, and never follows a symlink; a missing, corrupt or
unexpected store grants nothing.

Unresolved code operands, package runners and code-loading environment variables
are explicitly marked "code not pinned" with reasons in the prompt,
stored grant and trust-check warning. Their grant binds configuration and any
identified files only. Installed executables, external dependencies, dynamic
imports and shell/interpreter indirection are not transitively pinned. This is
a configuration and code-selection grant, not proof of process behavior.
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import errno
import fcntl
import hashlib
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path

from garuda.config_sources import record_digest, record_source
from garuda.mcp.launch_files import LaunchBindingError, discover_launch_files

logger = logging.getLogger(__name__)

STORE_VERSION = 1
BINDING_VERSION = 3
_MAX_SCRIPT_BYTES = 16 * 1024 * 1024
_MAX_STORE_BYTES = 4 * 1024 * 1024
UNTRUSTED_CODE = "agent.untrusted_project_code"


class TrustError(Exception):
    """The entry cannot be bound to stable content, so it cannot be trusted."""


@dataclass(frozen=True)
class TrustKey:
    repository: str
    name: str
    digest: str
    code_pinned: bool = True
    unpinned_reasons: tuple[str, ...] = ()
    bound_trees: tuple[str, ...] = ()


def trust_store_path() -> Path:
    from garuda.config.agent_home import global_settings_path

    return global_settings_path().expanduser().parent / "trust" / "mcp-servers.json"


def repository_identity(workspace: str | Path) -> str:
    return str(Path(workspace).expanduser().resolve())


def _within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _file_digest(path: Path) -> str:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    digest = hashlib.sha256()
    total = 0
    with os.fdopen(fd, "rb") as handle:
        while chunk := handle.read(1 << 16):
            total += len(chunk)
            if total > _MAX_SCRIPT_BYTES:
                raise TrustError(f"referenced file {path} is larger than {_MAX_SCRIPT_BYTES} bytes")
            digest.update(chunk)
    value = digest.hexdigest()
    record_digest(path, value)
    return value


def _referenced_files(paths: list[Path], workspace: Path) -> list[dict]:
    """Repository files the command line names, with how each one resolves."""
    refs: list[dict] = []
    for candidate in sorted(set(paths)):
        resolved = candidate.resolve()
        ref = {"token": str(candidate.relative_to(workspace)),
               "symlink": candidate.is_symlink(), "target": str(resolved)}
        if resolved.is_file() and _within(resolved, workspace):
            ref["sha256"] = _file_digest(resolved)
        refs.append(ref)
    return refs


def trust_key(server, workspace: str | Path) -> TrustKey:
    """Snapshot the entry and identified launch files for review and granting."""
    root = Path(repository_identity(workspace))
    try:
        files = discover_launch_files([server.command, *server.args], root, server.env)
        references = _referenced_files(files.paths, root)
    except (LaunchBindingError, RuntimeError) as exc:
        raise TrustError(str(exc)) from exc
    payload = {
        "version": STORE_VERSION,
        "binding_version": BINDING_VERSION,
        "name": server.name,
        "spec": server.spec_digest,
        "files": references,
        "code_pinned": not files.unpinned,
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return TrustKey(str(root), server.name, hashlib.sha256(blob).hexdigest(), not files.unpinned,
                    tuple(files.reasons), tuple(str(path.relative_to(root)) for path in files.trees))


def entry_digest(server, workspace: str | Path) -> str:
    """Digest of the entry, identified files and explicit binding status."""
    return trust_key(server, workspace).digest


def _read_store(path: Path) -> list[dict]:
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        record_source(path, None)
        return []
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            logger.warning("MCP trust store %s is a symlink; ignoring it", path)
            return []
        raise
    try:
        info = os.fstat(fd)
        if info.st_size > _MAX_STORE_BYTES:
            logger.warning("MCP trust store %s is too large; ignoring it", path)
            return []
        with os.fdopen(fd, "rb") as handle:
            fd = -1
            raw = handle.read()
            record_source(path, raw)
            data = json.loads(raw.decode("utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("MCP trust store %s is unreadable (%s); no project server is trusted", path, exc)
        return []
    finally:
        if fd >= 0:
            os.close(fd)
    if not isinstance(data, dict) or data.get("version") != STORE_VERSION:
        logger.warning("MCP trust store %s has an unsupported format; ignoring it", path)
        return []
    grants = data.get("grants")
    if not isinstance(grants, list):
        return []
    return [g for g in grants if isinstance(g, dict)]


def is_trusted(server, workspace: str | Path) -> bool:
    try:
        key = trust_key(server, workspace)
    except (TrustError, OSError) as exc:
        logger.warning("MCP server %r cannot be trusted: %s", server.name, exc)
        return False
    trusted = has_grant(key)
    if trusted and not key.code_pinned:
        logger.warning("MCP server %r: code not pinned (%s); grant covers configuration and identified files only",
                       server.name, "; ".join(key.unpinned_reasons))
    return trusted


def has_grant(key: TrustKey) -> bool:
    """Whether this exact identity, digest and binding status were granted.

    Changes to covered content invalidate a grant. An explicitly unpinned key
    authorizes its configuration/identified files, not unresolved code bytes.
    """
    for grant in _read_store(trust_store_path()):
        same_entry = grant.get("repository") == key.repository and grant.get("name") == key.name
        if same_entry and grant.get("binding_version") != BINDING_VERSION:
            logger.warning("MCP server %r: trust needs review after the binding update", key.name)
            continue
        if (
            grant.get("repository") == key.repository
            and grant.get("name") == key.name
            and grant.get("digest") == key.digest
            and grant.get("code_pinned", True) is key.code_pinned
        ):
            return True
    return False


def _ensure_private_dir(directory: Path) -> None:
    if directory.is_symlink():
        raise TrustError(f"{directory} is a symlink; refusing to write trust records there")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(directory, 0o700)


@contextlib.contextmanager
def _locked(directory: Path):
    fd = os.open(
        directory / ".lock",
        os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def grant(server, workspace: str | Path) -> TrustKey:
    """Record the user's trust in this exact entry for this repository."""
    return grant_key(trust_key(server, workspace))


def grant_key(key: TrustKey) -> TrustKey:
    """Record a grant for ``key``, replacing an older one of the same name."""
    path = trust_store_path()
    _ensure_private_dir(path.parent)
    with _locked(path.parent):
        grants = [
            g
            for g in _read_store(path)
            if not (g.get("repository") == key.repository and g.get("name") == key.name)
        ]
        grants.append(
            {
                "repository": key.repository,
                "name": key.name,
                "digest": key.digest,
                "code_pinned": key.code_pinned,
                "binding_version": BINDING_VERSION,
                "granted_at": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
            }
        )
        blob = json.dumps({"version": STORE_VERSION, "grants": grants}, indent=1).encode()
        tmp = path.parent / f".{path.name}.{os.getpid()}.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            os.write(fd, blob)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, path)
        dir_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    return key


def describe(server) -> str:
    """One line naming what trusting this server would allow."""
    if server.url:
        return f"connect to {server.url}"
    return "run " + " ".join([server.command, *server.args]).strip()
