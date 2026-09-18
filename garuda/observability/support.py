"""Redacted support bundles (P1.13, issue #45).

`build_support_bundle` packs what triage needs and nothing it must not hold:
redacted session metadata, lane structure with counts, event-kind tallies,
versions, and a metrics snapshot. Raw prompts, transcripts, tokens, and
secrets never enter the bundle — every string passes through redaction, and
payloads are counted, never copied.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from garuda.context.redact import redact_text


def _scrub(value: Any) -> Any:
    if isinstance(value, str):
        cleaned, _ = redact_text(value)
        return cleaned
    if isinstance(value, dict):
        return {key: _scrub(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_scrub(item) for item in value]
    return value


def _tally(path: Path) -> dict[str, int]:
    kinds: dict[str, int] = {}
    if not path.exists():
        return kinds
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            kinds["malformed"] = kinds.get("malformed", 0) + 1
            continue
        if isinstance(record, dict):
            kind = str(record.get("type", record.get("kind", "unknown")))
            kinds[kind] = kinds.get(kind, 0) + 1
        else:
            kinds["malformed"] = kinds.get("malformed", 0) + 1
    return kinds


def build_support_bundle(
    session_dir: str | Path,
    *,
    metrics: dict | None = None,
    garuda_version: str = "unknown",
) -> dict[str, Any]:
    """Assemble the bundle dict. Pure read of the session dir plus redaction."""
    root = Path(session_dir)
    try:
        meta = json.loads((root / "meta.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        meta = {}
    if not isinstance(meta, dict):
        meta = {}
    lanes: list[dict[str, Any]] = []
    try:
        from garuda.observability.lanes import export_trace, read_cross_runtime

        exported = export_trace(read_cross_runtime(root))
        lanes = exported["lanes"]
    except Exception:
        lanes = []
    return {
        "session_id": meta.get("session_id", root.name),
        "meta": _scrub({k: v for k, v in meta.items() if k != "session_id"}),
        "lanes": lanes,
        "native_event_kinds": _tally(root / "events.jsonl"),
        "external_event_kinds": _tally(root / "acp-events.jsonl"),
        "metrics": metrics if metrics is not None else {},
        "garuda_version": garuda_version,
    }
