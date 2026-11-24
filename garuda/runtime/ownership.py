"""Who owns a lease or a capacity slot, and whether that owner is still alive.

Shared by workspace leases, capacity reservations and the queue (plan task B.0,
issue #157). An owner is a process identified by:

- ``pid``;
- ``identity`` — its start identity from :func:`garuda.runtime.recovery._process_identity`
  (boot id plus start time on Linux, ``ps -o lstart=`` elsewhere), so a reused
  pid is never mistaken for the owner;
- ``pgid`` — its process group; when the owner leads it, a live descendant
  keeps the owner alive;
- ``epoch`` — a random token minted when ownership is taken, so a superseded
  owner's late write can be told apart from the current owner's.

Liveness is three-valued: ``True`` (alive), ``False`` (confirmed dead) and
``None`` (cannot tell). Only ``False`` ever permits taking something over.

Owner death alone does not prove that the owner's descendants are gone: a
child that started its own session survives the owner and may still write.
So an owner stamps a lineage token into its environment (``GARUDA_OWNER_LINEAGE``)
the moment it takes ownership, every process it launches afterwards inherits
it, and :func:`reclaim_refusal` refuses while any live process still carries
the dead owner's token (#676). A process launched with a scrubbed environment
is outside that scan; Garuda's own ACP launches pass the token on explicitly.
"""

from __future__ import annotations

import hashlib
import os
import uuid
from dataclasses import dataclass
from typing import Callable

Liveness = Callable[[dict], "bool | None"]

#: Space-separated lineage tokens of every owner this process descends from.
LINEAGE_ENV = "GARUDA_OWNER_LINEAGE"


@dataclass(frozen=True)
class Owner:
    pid: int
    identity: str
    pgid: int
    epoch: str

    def to_dict(self) -> dict:
        return {"pid": self.pid, "identity": self.identity, "pgid": self.pgid, "epoch": self.epoch}


def current_owner() -> Owner:
    """This process as an owner, with a fresh epoch."""
    from garuda.runtime.recovery import _process_identity

    pid = os.getpid()
    try:
        identity = _process_identity(pid) or "unknown"
    except Exception:
        identity = "unknown"
    _stamp_lineage(pid, identity)
    return Owner(pid=pid, identity=identity, pgid=os.getpgid(pid), epoch=uuid.uuid4().hex)


def lineage_token(pid: object, identity: object) -> str | None:
    """The token an owner's descendants inherit, or None without a usable identity."""
    if (not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0
            or not isinstance(identity, str) or identity in ("", "unknown")):
        return None
    return hashlib.sha256(f"{pid}\0{identity}".encode()).hexdigest()[:24]


def _stamp_lineage(pid: int, identity: str) -> None:
    token = lineage_token(pid, identity)
    if token is None:
        return
    tokens = os.environ.get(LINEAGE_ENV, "").split()
    if token not in tokens:
        os.environ[LINEAGE_ENV] = " ".join([*tokens, token])


def surviving_descendants(owner: dict) -> list[int] | None:
    """Live processes that inherited ``owner``'s lineage token.

    Reads each same-user process's start-time environment from ``/proc``.
    An owner from an earlier boot has none. A same-user process whose
    environment is unreadable (a non-dumpable one, such as an sshd session) is
    ruled out only when it, or a live ancestor of it below init, started
    before the owner; an orphan reparented to init has no such ancestor. (A
    non-dumpable orphan reparented to an older subreaper, such as
    ``systemd --user``, is outside the scan.) ``None`` means the scan is impossible or inconclusive (no
    ``/proc``, no Linux owner identity, or an unexplained unreadable
    process), so descendant cleanup stays unproved.
    """
    from garuda.runtime.recovery import _linux_stat

    token = lineage_token(owner.get("pid"), owner.get("identity"))
    identity = owner.get("identity")
    if (token is None or not os.path.isfile("/proc/self/environ")
            or not identity.startswith("linux:") or identity.count(":") != 2):
        return None
    _, boot_id, start = identity.split(":")
    try:
        owner_start = int(start)
        with open("/proc/sys/kernel/random/boot_id") as handle:
            current_boot = handle.read().strip()
    except (OSError, ValueError):
        return None
    if boot_id and current_boot and boot_id != current_boot:
        return []  # nothing started before this boot is still running
    uid = os.getuid()
    needle = f"{LINEAGE_ENV}=".encode()
    found = []
    try:
        entries = os.listdir("/proc")
    except OSError:
        return None
    for name in entries:
        if not name.isdigit():
            continue
        pid = int(name)
        try:
            if os.stat(f"/proc/{pid}").st_uid != uid:
                continue
            with open(f"/proc/{pid}/environ", "rb") as handle:
                environ = handle.read()
        except (FileNotFoundError, ProcessLookupError):
            continue  # exited while scanning
        except PermissionError:
            # A non-dumpable process: its environment is private even to
            # its own user. It is ruled out when it, or a live ancestor below
            # init, started before the owner. Every live ancestor of an
            # orphaned descendant below its reaper started after the owner.
            if not _older_lineage(pid, owner_start, _linux_stat):
                return None
            continue
        except OSError:
            if not _older_lineage(pid, owner_start, _linux_stat):
                return None
            continue
        for entry in environ.split(b"\0"):
            if entry.startswith(needle) and token in entry[len(needle):].decode(
                    "utf-8", "replace").split():
                found.append(pid)
                break
    return sorted(found)


def _older_lineage(pid: int, owner_start: int, stat) -> bool:
    """Whether ``pid`` or a live ancestor of it below init started before ``owner_start``."""
    from garuda.runtime.recovery import RecoveryError

    for _depth in range(256):
        if pid <= 1:
            return False
        try:
            fields = stat(pid)
            if fields is None:
                return False
            if int(fields[20]) < owner_start:
                return True
            pid = int(fields[2])
        except (RecoveryError, OSError, ValueError, IndexError):
            return False
    return False


def reclaim_refusal(owner: dict, *, quarantined: bool = False,
                    liveness: Liveness | None = None,
                    scan_descendants: bool = True) -> str | None:
    """Why a lease or slot held by ``owner`` cannot be reclaimed, or None if it can.

    Reclaim needs proof, never a guess: the holder was not quarantined by its
    owner, the owner is confirmed dead by identity (pid gone, or reused by
    another process; a group it led is empty) and no live process still
    carries its lineage token. A live owner is never evicted.
    ``scan_descendants=False`` is only a pre-check; reclaim always scans.
    """
    if quarantined:
        return ("its owner quarantined it because cleanup or a recorded phase was "
                "not reconciled")
    state = (liveness or owner_liveness)(owner)
    if state is True:
        return f"its owner (pid {owner.get('pid')}) or its process group is still running"
    if state is not False:
        return f"its owner's (pid {owner.get('pid')}) liveness cannot be determined"
    if not scan_descendants:
        return None
    survivors = surviving_descendants(owner)
    if survivors is None:
        return ("its owner is dead but its descendants cannot be inspected "
                "(needs /proc and a recorded owner identity)")
    if survivors:
        return ("its owner is dead but descendant process(es) "
                f"{', '.join(map(str, survivors))} are still running")
    return None


def owner_liveness(owner: dict) -> bool | None:
    """True = alive, False = confirmed dead, None = unknown.

    A live pid whose start identity differs from the recorded one has been
    reused by another process, so the recorded owner is dead. A record
    without an identity (written before identities were kept) can only be
    proven dead, never proven alive.
    """
    from garuda.runtime.recovery import _process_identity, _process_live, same_process

    pid = owner.get("pid")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return None
    recorded = owner.get("identity")
    try:
        live = _process_live(pid)
    except Exception:
        return None
    if live is None:
        return None
    if live:
        if not isinstance(recorded, str) or recorded in ("", "unknown"):
            return None
        try:
            current = _process_identity(pid)
        except Exception:
            return None
        if current is None:
            return None
        return same_process(recorded, current)
    # The leader is gone. Its process group says something about its
    # descendants only when it led that group; otherwise the group is shared
    # with unrelated processes (a shell, a test runner) and proves nothing.
    pgid = owner.get("pgid")
    if isinstance(pgid, int) and not isinstance(pgid, bool) and pgid == pid and pgid > 1:
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return None
        return True
    return False
