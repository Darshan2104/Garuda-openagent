"""Cross-runtime trace lanes (P1.9, issue #40).

A trace distinguishes native Garuda events from normalized external agent
events: each unified-session segment becomes a `RuntimeLane` carrying runtime
identity, native session refs, the authority snapshot, and the event ranges
belonging to it. Historical logs (no segments at all) read exactly as before
with zero lanes.

`export_trace` is the sharing format: lane structure, counts, handoff and
recovery state — never raw transcripts, reasoning, or secrets. Event payloads
stay out by construction: the exporter only ever reads kinds and counts.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ACP_EVENTS_FILE = "acp-events.jsonl"


@dataclass
class RuntimeLane:
    runtime_id: str
    kind: str
    native_session_id: str | None = None
    version: str = "unknown"
    authority: tuple[str, ...] = ()
    native_event_start: int | None = None
    native_event_end: int | None = None
    external_events: int = 0


@dataclass
class CrossRuntimeTrace:
    session_id: str
    lanes: list[RuntimeLane] = field(default_factory=list)
    handoff: dict[str, Any] = field(default_factory=dict)
    recovery_hint: str = ""
    external_kinds: dict[str, int] = field(default_factory=dict)

    def lane_for(self, runtime_id: str) -> RuntimeLane | None:
        return next((lane for lane in self.lanes if lane.runtime_id == runtime_id), None)


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def read_cross_runtime(session_dir: str | Path) -> CrossRuntimeTrace:
    """Build the lane view for one session directory.

    Reads `meta.json` (unified segments, handoff), `events.jsonl` (native
    trail, counted per lane via cursors), and `acp-events.jsonl` (normalized
    external events, counted by kind). Missing files mean absent data, never
    an error — that is what keeps historical logs readable.
    """
    root = Path(session_dir)
    try:
        meta = json.loads((root / "meta.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        meta = {}
    session_id = meta.get("session_id", root.name) if isinstance(meta, dict) else root.name
    segments = meta.get("runtime_segments", []) if isinstance(meta, dict) else []
    handoff = meta.get("handoff", {}) if isinstance(meta, dict) else {}

    native_count = len(_read_jsonl(root / "events.jsonl"))
    external = _read_jsonl(root / ACP_EVENTS_FILE)
    kinds: dict[str, int] = {}
    for record in external:
        kind = record.get("kind", "unknown")
        kinds[kind] = kinds.get(kind, 0) + 1

    lanes: list[RuntimeLane] = []
    cursor = 0
    for segment in segments:
        if not isinstance(segment, dict):
            continue
        end = segment.get("event_cursor", native_count)
        end = end if isinstance(end, int) and end >= 0 else native_count
        lanes.append(
            RuntimeLane(
                runtime_id=str(segment.get("runtime_id", "?")),
                kind=str(segment.get("kind", "?")),
                native_session_id=segment.get("native_session_id"),
                version=str(segment.get("version", "unknown")),
                authority=tuple(sorted(segment.get("capabilities", []))),
                native_event_start=cursor,
                native_event_end=min(end, native_count),
                external_events=len(external) if segment.get("kind") == "acp" else 0,
            )
        )
        cursor = min(end, native_count)

    hint = ""
    state = handoff.get("state", "none") if isinstance(handoff, dict) else "none"
    if state == "prepared":
        hint = "switch prepared but unacknowledged; source retained"
    elif state == "failed":
        hint = "last switch failed; source resumable"
    return CrossRuntimeTrace(
        session_id=session_id if isinstance(session_id, str) else root.name,
        lanes=lanes,
        handoff=dict(handoff) if isinstance(handoff, dict) else {},
        recovery_hint=hint,
        external_kinds=kinds,
    )


def export_trace(trace: CrossRuntimeTrace) -> dict[str, Any]:
    """Sharing-safe export: structure and counts only. No payloads, no transcripts."""
    return {
        "session_id": trace.session_id,
        "lanes": [
            {
                "runtime_id": lane.runtime_id,
                "kind": lane.kind,
                "native_session_id": lane.native_session_id,
                "version": lane.version,
                "authority": list(lane.authority),
                "native_events": (
                    None
                    if lane.native_event_start is None
                    else lane.native_event_end - lane.native_event_start
                ),
                "external_events": lane.external_events,
            }
            for lane in trace.lanes
        ],
        "handoff": trace.handoff,
        "recovery_hint": trace.recovery_hint,
        "external_kinds": dict(trace.external_kinds),
    }
