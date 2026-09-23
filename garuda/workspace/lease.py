"""Mutating-workspace leases and worktree isolation (P0.16, issue #26).

One workspace has at most one live mutating owner; read-only holders share
freely. A lease is a small JSON document in the agent-home leases dir (never
in the workspace itself), published atomically and refreshed by heartbeat.
Liveness is heartbeat TTL only — takeover replaces the lease file and nothing
else, so user changes are never removed to resolve a conflict. A corrupt lease
file fails closed: acquisition is refused until a human clears it.

Parallel worktrees are separate workspaces by real path: `workspace_key`
maps each worktree to its own lease, session, and context state.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import subprocess
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows has no fcntl
    fcntl = None  # type: ignore[assignment]

DEFAULT_TTL_SEC = 60.0


def _check_ttl(ttl_sec: float, *, where: str) -> float:
    """Validate a heartbeat TTL. Must be positive and finite.

    A zero/negative TTL is instantly stale (takeover on arrival); an infinite
    or NaN TTL never expires (a dead holder blocks the workspace forever).
    Both turn the safety guarantee into its opposite, so both fail closed.
    """
    try:
        ttl = float(ttl_sec)
    except (TypeError, ValueError) as exc:
        raise LeaseError(f"{where}: ttl_sec must be a number: {exc}") from exc
    if not math.isfinite(ttl) or ttl <= 0:
        raise LeaseError(f"{where}: ttl_sec must be positive and finite, got {ttl_sec!r}")
    return ttl


class LeaseError(Exception):
    """Base for lease failures. Conflicts carry the holder for audit."""


class LeaseConflictError(LeaseError):
    def __init__(self, message: str, *, holder: dict | None = None):
        super().__init__(message)
        self.holder = holder or {}


@dataclass(frozen=True)
class Lease:
    workspace: str
    session_id: str
    mode: str
    acquired_at: float
    heartbeat_at: float
    ttl_sec: float
    pid: int
    stolen_from: str = ""

    def is_stale(self, now: float | None = None) -> bool:
        return (now if now is not None else time.time()) - self.heartbeat_at > self.ttl_sec

    def to_dict(self) -> dict:
        return {
            "workspace": self.workspace,
            "session_id": self.session_id,
            "mode": self.mode,
            "acquired_at": self.acquired_at,
            "heartbeat_at": self.heartbeat_at,
            "ttl_sec": self.ttl_sec,
            "pid": self.pid,
            "stolen_from": self.stolen_from,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Lease":
        if not isinstance(data, dict):
            raise LeaseError("lease document must be a mapping")
        for key in ("workspace", "session_id", "mode"):
            if not isinstance(data.get(key), str) or not data[key]:
                raise LeaseError(f"lease.{key} must be a non-empty string")
        if data["mode"] not in ("mutating", "read-only"):
            raise LeaseError(f"lease.mode must be mutating|read-only, got {data['mode']!r}")
        ttl_sec = _check_ttl(data.get("ttl_sec", DEFAULT_TTL_SEC), where="lease")
        try:
            return cls(
                workspace=data["workspace"],
                session_id=data["session_id"],
                mode=data["mode"],
                acquired_at=float(data.get("acquired_at", 0)),
                heartbeat_at=float(data.get("heartbeat_at", 0)),
                ttl_sec=ttl_sec,
                pid=int(data.get("pid", 0)),
                stolen_from=str(data.get("stolen_from", "")),
            )
        except (TypeError, ValueError) as exc:
            raise LeaseError(f"lease has malformed times/pid: {exc}") from exc


def workspace_key(workspace: str | Path) -> str:
    """Isolation identity for a workspace: its canonical real path."""
    return os.path.realpath(os.path.expanduser(str(workspace)))


def default_leases_root() -> Path:
    override = os.environ.get("GARUDA_LEASES_DIR")
    if override:
        return Path(override).expanduser()
    from garuda.config.agent_home import global_home_dir

    return global_home_dir() / "leases"


class LeaseStore:
    """Issues and tracks workspace leases under one root directory."""

    def __init__(self, root: str | Path | None = None):
        self.root = Path(root) if root else default_leases_root()

    @contextmanager
    def _locked(self) -> Iterator[None]:
        """Serialize read-check-publish cycles across processes.

        Fail-closed: without the lock two racers both read an empty slot and
        both publish, and the loser never knows — the mutual-exclusion promise
        would be fiction. So a platform without `fcntl`, or a filesystem where
        locking fails, refuses acquisition instead of proceeding unlocked.
        """
        if fcntl is None:
            raise LeaseError("lease locking unavailable on this platform (no fcntl)")
        lock_path = self.root / ".lock"
        try:
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            handle = open(lock_path, "a+")
        except OSError as exc:
            raise LeaseError(f"cannot open lease lock at {lock_path}: {exc}") from exc
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        except OSError as exc:
            handle.close()
            raise LeaseError(f"cannot lock {lock_path}: {exc}") from exc
        try:
            yield
        finally:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()

    def _path_for(self, key: str) -> Path:
        digest = hashlib.sha256(key.encode()).hexdigest()[:32]
        return self.root / f"{digest}.json"

    def _read_all(self, path: Path) -> list[Lease]:
        if not path.exists():
            return []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise LeaseError(f"unreadable lease at {path}: {exc}; refusing") from exc
        if not isinstance(data, dict) or not isinstance(data.get("holders"), list):
            raise LeaseError(f"unreadable lease at {path}: bad holders; refusing")
        try:
            return [Lease.from_dict(entry) for entry in data["holders"]]
        except LeaseError as exc:
            raise LeaseError(f"unreadable lease at {path}: {exc}; refusing") from exc

    def _publish_all(self, path: Path, holders: list[Lease]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        tmp.write_text(
            json.dumps({"version": 1, "holders": [h.to_dict() for h in holders]}, indent=2),
            encoding="utf-8",
        )
        os.replace(tmp, path)

    def acquire(
        self,
        workspace: str | Path,
        session_id: str,
        mode: str = "mutating",
        *,
        ttl_sec: float = DEFAULT_TTL_SEC,
        now: float | None = None,
    ) -> Lease:
        """Take a lease. A live foreign mutating lease refuses; a stale one is
        taken over (recorded in `stolen_from`); read-only holders never block."""
        if mode not in ("mutating", "read-only"):
            raise LeaseError(f"mode must be mutating|read-only, got {mode!r}")
        if not session_id:
            raise LeaseError("session_id is required")
        ttl_sec = _check_ttl(ttl_sec, where="acquire")
        key = workspace_key(workspace)
        moment = now if now is not None else time.time()
        path = self._path_for(key)
        with self._locked():
            holders = self._read_all(path)
            live = [h for h in holders if not h.is_stale(moment)]
            foreign = [h for h in live if h.session_id != session_id]
            if (
                mode == "mutating"
                and any(h.mode == "mutating" for h in foreign)
            ):
                holder = next(h for h in foreign if h.mode == "mutating")
                raise LeaseConflictError(
                    f"workspace {key} is mutably held by session {holder.session_id}",
                    holder=holder.to_dict(),
                )
            stolen = sorted(
                {h.session_id for h in holders if h.is_stale(moment)}
                - {session_id}
            )
            kept = [h for h in live if h.session_id != session_id]
            lease = Lease(
                workspace=key,
                session_id=session_id,
                mode=mode,
                acquired_at=moment,
                heartbeat_at=moment,
                ttl_sec=ttl_sec,
                pid=os.getpid(),
                stolen_from=",".join(stolen),
            )
            self._publish_all(path, [*kept, lease])
            return lease

    def heartbeat(self, workspace: str | Path, session_id: str) -> Lease:
        """Refresh our lease. Foreign or missing leases fail closed."""
        key = workspace_key(workspace)
        path = self._path_for(key)
        with self._locked():
            holders = self._read_all(path)
            current = next((h for h in holders if h.session_id == session_id), None)
            if current is None:
                live_holder = next((h for h in holders if not h.is_stale()), None)
                if live_holder is not None:
                    raise LeaseConflictError(
                        f"workspace {key} is held by session {live_holder.session_id}",
                        holder=live_holder.to_dict(),
                    )
                raise LeaseError(f"no lease for workspace {key}")
            refreshed = Lease(
                workspace=current.workspace,
                session_id=current.session_id,
                mode=current.mode,
                acquired_at=current.acquired_at,
                heartbeat_at=time.time(),
                ttl_sec=current.ttl_sec,
                pid=os.getpid(),
            )
            self._publish_all(
                path, [h if h.session_id != session_id else refreshed for h in holders]
            )
            return refreshed

    def release(self, workspace: str | Path, session_id: str) -> None:
        """Release our holder entry. Releasing another session's entry is refused."""
        key = workspace_key(workspace)
        path = self._path_for(key)
        with self._locked():
            holders = self._read_all(path)
            if not any(h.session_id == session_id for h in holders):
                live_holder = next((h for h in holders if not h.is_stale()), None)
                if live_holder is not None:
                    raise LeaseConflictError(
                        f"cannot release workspace {key} held by session {live_holder.session_id}",
                        holder=live_holder.to_dict(),
                    )
                return
            self._publish_all(path, [h for h in holders if h.session_id != session_id])

    def holders_of(self, workspace: str | Path) -> list[Lease]:
        """Inspect current holders, if any. Never mutates."""
        return self._read_all(self._path_for(workspace_key(workspace)))


def is_worktree(path: str | Path) -> bool:
    """True when `path` is inside a git worktree (main or linked)."""
    result = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "--is-inside-work-tree"],
        capture_output=True,
        text=True,
        timeout=15,
    )
    return result.returncode == 0 and result.stdout.strip() == "true"


def create_worktree(repo: str | Path, path: str | Path, *, branch: str | None = None) -> Path:
    """Create a linked worktree for parallel runs. Never touches existing files."""
    target = Path(path)
    if target.exists():
        raise LeaseError(f"worktree target already exists: {target}")
    command = ["git", "-C", str(repo), "worktree", "add", str(target)]
    if branch:
        command.append(branch)
    result = subprocess.run(command, capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        raise LeaseError(f"git worktree add failed: {result.stderr.strip()}")
    return target
