"""Cross-runtime trace lanes (P1.9, issue #40).

A trace distinguishes native Garuda events from normalized external agent
events: each unified-session segment becomes a `RuntimeLane` carrying runtime
identity, native session refs, the authority snapshot, and the event ranges
belonging to it. Historical logs (no segments at all) read exactly as before
with zero lanes.

`export_trace` is the sharing format: a versioned, whitelisted projection —
lane structure, counts, handoff and recovery state — never raw transcripts,
reasoning, or secrets. Every exported string passes best-effort secret
scrubbing, and only known keys are emitted, so adversarial content in
session ids, handoff fields, or authority entries cannot leak through an
invented shape.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from garuda.context.redact import redact_text

ACP_EVENTS_FILE = "acp-events.jsonl"

#: Version of the sharing format. Readers reject newer versions they cannot
#: interpret instead of guessing at unknown fields.
TRACE_EXPORT_VERSION = 1

#: Handoff keys allowed into the export. Anything else a future handoff
#: record carries stays local.
_HANDOFF_KEYS = frozenset(
    {"state", "attempts", "target_runtime", "baseline_commit", "reason", "note"}
)


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
    acp_segments = [
        segment for segment in segments
        if isinstance(segment, dict) and segment.get("kind") == "acp"
    ]
    cursor = 0
    for segment in segments:
        if not isinstance(segment, dict):
            continue
        end = segment.get("event_cursor", native_count)
        end = end if isinstance(end, int) and end >= 0 else native_count
        is_acp = segment.get("kind") == "acp"
        segment_id = segment.get("native_session_id")
        matching_external = [
            record for record in external
            if record.get("segment_id") == segment_id
        ]
        # Older persisted trails predate segment ids. They remain readable
        # when there is only one ACP tenure; ambiguous multi-tenure data is
        # intentionally not attributed to every lane.
        if is_acp and not matching_external and len(acp_segments) == 1:
            matching_external = external
        lanes.append(
            RuntimeLane(
                runtime_id=str(segment.get("runtime_id", "?")),
                kind=str(segment.get("kind", "?")),
                native_session_id=segment.get("native_session_id"),
                version=str(segment.get("version", "unknown")),
                authority=tuple(sorted(segment.get("capabilities", []))),
                native_event_start=None if is_acp else cursor,
                native_event_end=None if is_acp else min(end, native_count),
                external_events=len(matching_external) if is_acp else 0,
            )
        )
        if not is_acp:
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


def _clean_string(value: object) -> str:
    """Best-effort secret scrub for one exported string. Non-strings become ""."""
    if not isinstance(value, str):
        return ""
    cleaned, _ = redact_text(value)
    return cleaned


def _clean_handoff(handoff: dict[str, Any]) -> dict[str, Any]:
    """Whitelisted, scrubbed handoff projection. Unknown keys never export."""
    if not isinstance(handoff, dict):
        return {}
    projected: dict[str, Any] = {}
    for key in ("state", "target_runtime", "baseline_commit", "reason", "note"):
        if key in handoff:
            projected[key] = _clean_string(handoff[key])
    if "attempts" in handoff and isinstance(handoff["attempts"], int):
        projected["attempts"] = handoff["attempts"]
    return projected


def export_trace(trace: CrossRuntimeTrace) -> dict[str, Any]:
    """Sharing-safe export: versioned structure and counts only.

    Strict payload-free whitelist: lane identity strings are scrubbed,
    the handoff projects known keys only, and no payload, transcript, or
    reasoning text is read at any point.
    """
    return {
        "version": TRACE_EXPORT_VERSION,
        "session_id": _clean_string(trace.session_id),
        "lanes": [
            {
                "runtime_id": _clean_string(lane.runtime_id),
                "kind": _clean_string(lane.kind),
                "native_session_id": _clean_string(lane.native_session_id)
                if lane.native_session_id
                else None,
                "version": _clean_string(lane.version),
                "authority": [
                    _clean_string(entry) for entry in lane.authority
                ],
                "native_events": (
                    None
                    if lane.native_event_start is None
                    else lane.native_event_end - lane.native_event_start
                ),
                "external_events": lane.external_events
                if isinstance(lane.external_events, int)
                else 0,
            }
            for lane in trace.lanes
        ],
        "handoff": _clean_handoff(trace.handoff),
        "recovery_hint": _clean_string(trace.recovery_hint),
        "external_kinds": {
            _clean_string(kind): count
            for kind, count in trace.external_kinds.items()
            if isinstance(count, int)
        },
    }
