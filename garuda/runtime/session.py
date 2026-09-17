"""Unified session schema and legacy migration (P0.6, issue #13).

A Garuda session maps to one active runtime session and records runtime
identity, native session ids, capability snapshots, workspace baseline, handoff
state, and event cursors. Legacy native sessions (no schema version) keep
resuming: migration builds the upgraded document in memory and the caller
publishes it through the existing atomic locked meta path — a failed migration
or write always leaves the original file readable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from garuda.runtime.protocol import AgentRuntimeError

#: Schema version written by this code. Legacy sessions carry no version.
SESSION_SCHEMA_VERSION = 1

#: Handoff transaction states. Unknown values are rejected, never defaulted.
HANDOFF_STATES = frozenset({"none", "prepared", "acknowledged", "failed"})


class UnifiedSessionError(AgentRuntimeError):
    """Unknown schema version or malformed unified session document."""


@dataclass(frozen=True)
class RuntimeSegment:
    """One runtime's tenure within a session. The first segment is native."""

    runtime_id: str
    kind: str
    native_session_id: str | None = None
    version: str = "unknown"
    capabilities: frozenset[str] = field(default_factory=frozenset)
    event_cursor: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "runtime_id": self.runtime_id,
            "kind": self.kind,
            "native_session_id": self.native_session_id,
            "version": self.version,
            "capabilities": sorted(self.capabilities),
            "event_cursor": self.event_cursor,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any], *, where: str) -> "RuntimeSegment":
        if not isinstance(data, dict):
            raise UnifiedSessionError(f"{where}: runtime segment must be a mapping")
        for key in ("runtime_id", "kind"):
            if not isinstance(data.get(key), str) or not data[key]:
                raise UnifiedSessionError(f"{where}.{key}: must be a non-empty string")
        capabilities = data.get("capabilities", [])
        if not isinstance(capabilities, list) or any(
            not isinstance(c, str) for c in capabilities
        ):
            raise UnifiedSessionError(f"{where}.capabilities: must be a list of names")
        cursor = data.get("event_cursor", 0)
        if not isinstance(cursor, int) or cursor < 0:
            raise UnifiedSessionError(f"{where}.event_cursor: must be >= 0")
        return cls(
            runtime_id=data["runtime_id"],
            kind=data["kind"],
            native_session_id=data.get("native_session_id"),
            version=data.get("version", "unknown") or "unknown",
            capabilities=frozenset(capabilities),
            event_cursor=cursor,
        )


@dataclass(frozen=True)
class UnifiedSession:
    """Validated view of a session meta document with runtime segments."""

    session_id: str
    schema_version: int
    segments: tuple[RuntimeSegment, ...]
    baseline: dict[str, Any] = field(default_factory=dict)
    handoff: dict[str, Any] = field(default_factory=dict)
    legacy: dict[str, Any] = field(default_factory=dict)

    @property
    def active(self) -> RuntimeSegment:
        return self.segments[-1]


def _handoff_state(data: object, *, where: str) -> dict[str, Any]:
    if data is None:
        return {"state": "none", "attempts": 0}
    if not isinstance(data, dict):
        raise UnifiedSessionError(f"{where}: handoff must be a mapping")
    state = data.get("state", "none")
    if state not in HANDOFF_STATES:
        raise UnifiedSessionError(f"{where}.state: unknown handoff state {state!r}")
    attempts = data.get("attempts", 0)
    if not isinstance(attempts, int) or attempts < 0:
        raise UnifiedSessionError(f"{where}.attempts: must be >= 0")
    extra = {k: v for k, v in data.items() if k not in ("state", "attempts")}
    return {"state": state, "attempts": attempts, **extra}


def migrate_legacy_meta(meta: dict[str, Any]) -> dict[str, Any]:
    """Build the unified document for a legacy meta. Never mutates `meta`.

    The legacy Garuda session id becomes the native session id of the first
    (native) segment — it is the only stable identity a pre-runtime session has.
    """
    if not isinstance(meta, dict):
        raise UnifiedSessionError("session meta must be a mapping")
    if "session_id" not in meta:
        raise UnifiedSessionError("session meta has no session_id")
    upgraded = dict(meta)
    upgraded["schema_version"] = SESSION_SCHEMA_VERSION
    upgraded["runtime_segments"] = [
        {
            "runtime_id": "native",
            "kind": "native",
            "native_session_id": meta["session_id"],
            "version": "legacy",
            "capabilities": [],
            "event_cursor": 0,
        }
    ]
    upgraded.setdefault("baseline", {})
    upgraded.setdefault("handoff", {"state": "none", "attempts": 0})
    return upgraded


def load_unified(meta: dict[str, Any]) -> UnifiedSession:
    """Validate a meta document into a `UnifiedSession`.

    Unversioned documents migrate in memory; unknown *future* versions fail with
    an actionable error. The input is never modified.
    """
    if not isinstance(meta, dict):
        raise UnifiedSessionError("session meta must be a mapping")
    version = meta.get("schema_version")
    if version is None:
        meta = migrate_legacy_meta(meta)
        version = SESSION_SCHEMA_VERSION
    if version != SESSION_SCHEMA_VERSION:
        raise UnifiedSessionError(
            f"session schema v{version} is newer than supported v{SESSION_SCHEMA_VERSION}; "
            "upgrade Garuda to read this session"
        )
    raw_segments = meta.get("runtime_segments", [])
    if not isinstance(raw_segments, list) or not raw_segments:
        raise UnifiedSessionError("unified session requires a non-empty runtime_segments list")
    segments = tuple(
        RuntimeSegment.from_dict(seg, where=f"runtime_segments[{i}]")
        for i, seg in enumerate(raw_segments)
    )
    baseline = meta.get("baseline", {})
    if not isinstance(baseline, dict):
        raise UnifiedSessionError("baseline must be a mapping")
    handoff = _handoff_state(meta.get("handoff"), where="handoff")
    session_id = meta.get("session_id", "")
    if not isinstance(session_id, str) or not session_id:
        raise UnifiedSessionError("session meta has no session_id")
    return UnifiedSession(
        session_id=session_id,
        schema_version=version,
        segments=segments,
        baseline=dict(baseline),
        handoff=handoff,
        legacy={k: v for k, v in meta.items() if k not in ("runtime_segments", "baseline", "handoff")},
    )
