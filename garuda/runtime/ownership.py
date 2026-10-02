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
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from typing import Callable

Liveness = Callable[[dict], "bool | None"]


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
    return Owner(pid=pid, identity=identity, pgid=os.getpgid(pid), epoch=uuid.uuid4().hex)


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
