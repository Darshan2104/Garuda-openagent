"""Mutating-workspace leases and worktree isolation (P0.16, issue #26).

One workspace has at most one live mutating owner; read-only holders share
freely. A lease is a small JSON document in the agent-home leases dir (never
in the workspace itself), published atomically and refreshed by heartbeat.
A heartbeat TTL marks a lease expired, but expiry alone never permits takeover:
the owner (pid, start identity, process group) must be confirmed dead.
Takeover replaces the lease file and nothing else, so user changes are never
removed to resolve a conflict. A corrupt lease
file fails closed: acquisition is refused until a human clears it.

Parallel worktrees are separate workspaces by real path: `workspace_key`
maps each worktree to its own lease, session, and context state.
"""

from __future__ import annotations

import hashlib
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
    # Owner identity (v2). Empty in leases written before identities were kept;
    # such an owner can be proven dead but never proven alive.
    identity: str = ""
    pgid: int = 0
    epoch: str = ""

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
            "identity": self.identity,
            "pgid": self.pgid,
            "epoch": self.epoch,
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
                identity=str(data.get("identity", "")),
                pgid=int(data.get("pgid", 0)),
                epoch=str(data.get("epoch", "")),
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
    """Issues and tracks workspace leases under one root directory.

    Storage is strict (``garuda.runtime.strict_store``): an owner-only
    directory, an exclusive no-follow lock, and atomic fsynced writes. A lease
    records its owner's pid, start identity, process group and an epoch. An
    expired lease is taken over only when its owner is *confirmed* dead — a
    live owner past its TTL keeps it, and an owner whose liveness cannot be
    determined blocks takeover until a person clears it.
    """

    def __init__(self, root: str | Path | None = None, *, liveness=None, owner_factory=None):
        from garuda.runtime.ownership import current_owner, owner_liveness

        self.root = Path(root) if root else default_leases_root()
        self._liveness = liveness or owner_liveness
        self._owner_factory = owner_factory or current_owner

    @contextmanager
    def _locked(self) -> Iterator[None]:
        """Serialize read-check-publish cycles across processes.

        Fail-closed: without the lock two racers both read an empty slot and
        both publish, and the loser never knows. A platform without `fcntl`, a
        symlinked directory or lock file, or a filesystem where locking fails
        refuses instead of proceeding unlocked.
        """
        from garuda.runtime.strict_store import StorageError, exclusive_lock

        if fcntl is None:
            raise LeaseError("lease locking unavailable on this platform (no fcntl)")
        try:
            with exclusive_lock(self.root):
                yield
        except StorageError as exc:
            raise LeaseError(f"lease storage unavailable: {exc}") from exc

    def _path_for(self, key: str) -> Path:
        digest = hashlib.sha256(key.encode()).hexdigest()[:32]
        return self.root / f"{digest}.json"

    def _read_all(self, path: Path) -> list[Lease]:
        from garuda.runtime.strict_store import StorageError, read_document

        try:
            data = read_document(path, versions=(1, 2))
        except StorageError as exc:
            raise LeaseError(f"unreadable lease at {path}: {exc}") from exc
        if data is None:
            return []
        if not isinstance(data.get("holders"), list):
            raise LeaseError(f"unreadable lease at {path}: bad holders; refusing")
        try:
            return [Lease.from_dict(entry) for entry in data["holders"]]
        except LeaseError as exc:
            raise LeaseError(f"unreadable lease at {path}: {exc}; refusing") from exc

    def _publish_all(self, path: Path, holders: list[Lease]) -> None:
        from garuda.runtime.strict_store import write_document

        write_document(path, {"version": 2, "holders": [h.to_dict() for h in holders]})

    def _owner_state(self, holder: Lease) -> bool | None:
        """Liveness of a holder's owner: True alive, False dead, None unknown."""
        return self._liveness(
            {"pid": holder.pid, "identity": holder.identity, "pgid": holder.pgid}
        )

    def acquire(
        self,
        workspace: str | Path,
        session_id: str,
        mode: str = "mutating",
        *,
        ttl_sec: float = DEFAULT_TTL_SEC,
        now: float | None = None,
    ) -> Lease:
        """Take a lease. A live foreign mutating holder refuses; an expired one
        is taken over only when its owner is confirmed dead (recorded in
        `stolen_from`); read-only holders never block."""
        if mode not in ("mutating", "read-only"):
            raise LeaseError(f"mode must be mutating|read-only, got {mode!r}")
        if not session_id:
            raise LeaseError("session_id is required")
        ttl_sec = _check_ttl(ttl_sec, where="acquire")
        key = workspace_key(workspace)
        moment = now if now is not None else time.time()
        path = self._path_for(key)
        owner = self._owner_factory()
        with self._locked():
            holders = self._read_all(path)
            kept: list[Lease] = []
            stolen: list[str] = []
            for holder in holders:
                if holder.session_id == session_id:
                    continue
                if not holder.is_stale(moment):
                    kept.append(holder)
                    continue
                state = self._owner_state(holder)
                if state is False:
                    stolen.append(holder.session_id)
                    continue
                if holder.mode == "mutating" and mode == "mutating":
                    reason = (
                        "its owner is still running past the lease TTL"
                        if state
                        else "its owner's liveness cannot be determined; clear it once "
                        "you have confirmed the owner is gone"
                    )
                    raise LeaseConflictError(
                        f"workspace {key} is mutably held by session {holder.session_id}: "
                        f"{reason}",
                        holder=holder.to_dict(),
                    )
                kept.append(holder)
            if mode == "mutating" and any(h.mode == "mutating" for h in kept):
                holder = next(h for h in kept if h.mode == "mutating")
                raise LeaseConflictError(
                    f"workspace {key} is mutably held by session {holder.session_id}",
                    holder=holder.to_dict(),
                )
            lease = Lease(
                workspace=key,
                session_id=session_id,
                mode=mode,
                acquired_at=moment,
                heartbeat_at=moment,
                ttl_sec=ttl_sec,
                pid=owner.pid,
                stolen_from=",".join(sorted(set(stolen))),
                identity=owner.identity,
                pgid=owner.pgid,
                epoch=owner.epoch,
            )
            self._publish_all(path, [*kept, lease])
            return lease

    def _own_entry(self, holders: list[Lease], session_id: str, epoch: str | None, key: str):
        current = next((h for h in holders if h.session_id == session_id), None)
        if current is not None and epoch is not None and current.epoch and current.epoch != epoch:
            raise LeaseConflictError(
                f"workspace {key}: this lease was superseded (owner epoch changed)",
                holder=current.to_dict(),
            )
        return current

    def heartbeat(
        self, workspace: str | Path, session_id: str, *, epoch: str | None = None
    ) -> Lease:
        """Refresh our lease. Foreign, missing or superseded leases fail closed."""
        key = workspace_key(workspace)
        path = self._path_for(key)
        with self._locked():
            holders = self._read_all(path)
            current = self._own_entry(holders, session_id, epoch, key)
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
                pid=current.pid,
                identity=current.identity,
                pgid=current.pgid,
                epoch=current.epoch,
            )
            self._publish_all(
                path, [h if h.session_id != session_id else refreshed for h in holders]
            )
            return refreshed

    def release(
        self, workspace: str | Path, session_id: str, *, epoch: str | None = None
    ) -> None:
        """Release our holder entry. Releasing another session's (or a
        superseded owner's) entry is refused."""
        key = workspace_key(workspace)
        path = self._path_for(key)
        with self._locked():
            holders = self._read_all(path)
            current = self._own_entry(holders, session_id, epoch, key)
            if current is None:
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

    def possibly_live_holders(self, *, now: float | None = None) -> list[Lease]:
        """Every lease, in any workspace, whose owner may still be running.

        Unexpired leases, and expired ones whose owner is not confirmed dead.
        Never mutates; a corrupt lease file raises (fail closed).
        """
        if not self.root.is_dir():
            return []
        moment = now if now is not None else time.time()
        live: list[Lease] = []
        for path in sorted(self.root.glob("*.json")):
            for holder in self._read_all(path):
                if not holder.is_stale(moment) or self._owner_state(holder) is not False:
                    live.append(holder)
        return live

    def live_holders_for_session(
        self, session_id: str, *, now: float | None = None
    ) -> list[Lease]:
        """Every lease of `session_id` whose owner may still be running.

        Restart recovery asks this before touching a session. A lease counts
        while it is unexpired, or expired but its owner is not confirmed dead.
        Never mutates; a corrupt lease file raises (fail closed), exactly as
        acquisition does.
        """
        if not self.root.is_dir():
            return []
        moment = now if now is not None else time.time()
        live: list[Lease] = []
        for path in sorted(self.root.glob("*.json")):
            for holder in self._read_all(path):
                if holder.session_id != session_id:
                    continue
                if not holder.is_stale(moment) or self._owner_state(holder) is not False:
                    live.append(holder)
        return live


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
