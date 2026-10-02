"""Account-bound provider-limit observations (plan task E.2, #168).

A limit status is recorded only from a source the usage-source spike (A.6) **proved** for
the exact harness version; for anything else the window is ``unknown``. Garuda never reads
credential or auth files, browser cookies or undocumented endpoints, and never sends a prompt.

An observation holds: the harness and exact version, the observation time, the source, the
windows (used fraction, length, reported reset), whether a limit is *reached*, the reported
reset time, and — only when the official interface supplied one — an **account digest**: the
id hashed with a salt that lives only in this user's Garuda home, so the digest is stable on
this machine and cannot be reversed or linked elsewhere. Account names are never stored.

**What may trigger fallback.** Exhaustion is *eligible* for the quota reason (C.9's
``harness.limit_reached``) only when all of these hold at selection time:

* the source is proved for this exact version, and the version has not changed since the
  previous reading;
* the reading — a refresh made just before selection — names an account, and it is the
  account the previous reading named;
* it is fresh — at most 60 seconds old;
* it reports the limit reached, with an explicit reset time still in the future.

Anything else — no account id, a changed account, version or login, a stale reading, an
unknown or past reset — leaves the harness **eligible**: the observation is display
evidence only. An unknown reset time never bans a provider indefinitely.

Snapshots (the latest observation per harness and per account/profile) are replaced
atomically, so a reader never sees a partial one; events are appended to the usage ledger,
which drops a repeat.
"""

from __future__ import annotations

import hashlib
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from garuda.observability.ledger import LIMIT, Ledger
from garuda.runtime import strict_store as ss

FALLBACK_TTL = 60.0
#: Sources proved by A.6, per harness: the exact versions they were proved for.
PROVED_SOURCES: dict[str, tuple[str, ...]] = {"codex": ("0.159.3",)}


@dataclass(frozen=True)
class Window:
    name: str
    used_fraction: float | None
    resets_at: float | None
    window_minutes: int | None = None


@dataclass(frozen=True)
class LimitObservation:
    harness: str
    harness_version: str | None
    observed_at: float
    source: str
    account_digest: str | None = None
    windows: tuple[Window, ...] = field(default_factory=tuple)
    limit_id: str | None = None
    reached: bool | None = None
    reset_at: float | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> LimitObservation:
        windows = tuple(Window(**w) for w in data.get("windows", []))
        return cls(**{**{k: v for k, v in data.items() if k != "windows"}, "windows": windows})


@dataclass(frozen=True)
class Decision:
    eligible_exhausted: bool
    reason: str


def default_root() -> Path:
    from garuda.config.agent_home import global_settings_path

    return global_settings_path().expanduser().parent / "limits"


def exact_version(text: str | None) -> str | None:
    """``codex-cli 0.159.3`` -> ``0.159.3`` (the last dotted version in the text)."""
    import re

    found = re.findall(r"\d+\.\d+\.\d+(?:[-+.\w]*)?", text or "")
    return found[-1] if found else None


class LimitStore:
    def __init__(self, root: str | Path | None = None, *, ledger: Ledger | None = None,
                 clock=time.time):
        self.root = Path(root) if root else default_root()
        self.ledger = ledger or Ledger()
        self._clock = clock

    # --- the account digest -----------------------------------------------------------

    def _salt(self) -> bytes:
        path = self.root / ".salt"
        with ss.exclusive_lock(self.root):
            try:
                fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            except FileNotFoundError:
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                             0o600)
                os.write(fd, os.urandom(32))
                os.close(fd)
                fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            try:
                return os.read(fd, 64)
            finally:
                os.close(fd)

    def account_digest(self, official_id: str | None) -> str | None:
        """A stable, local, irreversible stand-in for an officially supplied id."""
        if not official_id:
            return None
        return "acct:" + hashlib.sha256(self._salt() + str(official_id).encode()).hexdigest()[:16]

    # --- recording --------------------------------------------------------------------

    def _snapshot_path(self, harness: str, account: str | None = None) -> Path:
        name = f"harness-{harness}" if account is None else f"profile-{harness}-{account[5:]}"
        return self.root / f"{name}.json"

    def record(self, observation: LimitObservation) -> bool:
        """Replace the snapshots and, if a limit is reached, append its event. Returns
        whether an event was newly written."""
        try:
            with ss.exclusive_lock(self.root):
                ss.write_document(self._snapshot_path(observation.harness),
                                  {"version": 1, **observation.to_dict()})
                if observation.account_digest:
                    ss.write_document(
                        self._snapshot_path(observation.harness, observation.account_digest),
                        {"version": 1, **observation.to_dict()})
        except ss.StorageError:
            return False
        if not observation.reached:
            return False
        key = "limit:" + hashlib.sha256("|".join(str(p) for p in (
            observation.harness, observation.account_digest, observation.limit_id,
            observation.reset_at)).encode()).hexdigest()[:32]
        return self.ledger.append({
            "kind": LIMIT, "key": key, "time": observation.observed_at,
            "harness": observation.harness, "adapter_version": _version_token(observation.harness_version),
            "source": _token(observation.source), "limit_id": _token(observation.limit_id),
            "account_digest": observation.account_digest, "reset_at": observation.reset_at,
            "reason": "limit_reached"})

    def latest(self, harness: str, account: str | None = None) -> LimitObservation | None:
        document = ss.read_document(self._snapshot_path(harness, account), versions=(1,))
        if not document:
            return None
        document.pop("version", None)
        return LimitObservation.from_dict(document)

    # --- deciding ---------------------------------------------------------------------

    def assess(self, harness: str, refresh, *, now: float | None = None) -> Decision:
        """Refresh, record, and decide. ``refresh()`` returns the harness's reading *now*
        (or ``None``); the reading before it is what continuity is checked against."""
        prior = self.latest(harness)
        current = refresh()
        if current is not None:
            self.record(current)
        return self.decide(harness, current, prior=prior, now=now)

    def decide(self, harness: str, current: LimitObservation | None, *,
               prior: LimitObservation | None = None, now: float | None = None) -> Decision:
        """May ``current`` (a refresh made just now) make ``harness`` count as exhausted for
        fallback? ``prior`` is the reading before it, if any: a change of account or
        version since then invalidates eligibility until a later refresh confirms the new
        one."""
        now = self._clock() if now is None else now
        if current is None:
            return Decision(False, "the limit could not be refreshed")
        if current.harness_version not in PROVED_SOURCES.get(harness, ()):
            return Decision(False, "no source is proved for this version")
        if not current.account_digest:
            return Decision(False, "no account id: the observation is historical")
        if prior is not None and prior.account_digest and (
                prior.account_digest != current.account_digest):
            return Decision(False, "the account changed")
        if prior is not None and prior.harness_version != current.harness_version:
            return Decision(False, "the version changed")
        if now - current.observed_at > FALLBACK_TTL:
            return Decision(False, "the reading is stale")
        if not current.reached:
            return Decision(False, "the limit is not reached")
        if current.reset_at is None:
            return Decision(False, "no reset time reported: historical, not a ban")
        if current.reset_at <= now:
            return Decision(False, "the reset time has passed")
        return Decision(True, "same account, fresh, reached, reset in the future")


def _token(value):
    import re

    return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:@+=/-]{0,127}",
                                                            value) else None


def _version_token(value):
    return _token(value)


def event_state(record: dict, now: float) -> str:
    """How a ``limit_event`` reads on a card: ``active`` (reset still ahead),
    ``expired`` (reset passed) or ``historical`` (no reset was reported)."""
    reset = record.get("reset_at")
    if reset is None:
        return "historical"
    return "active" if reset > now else "expired"
