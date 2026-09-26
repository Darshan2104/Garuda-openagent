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

_PUBLIC_META_FIELDS = frozenset(
    {
        "session_id", "schema_version", "status", "created_at", "updated_at",
        "turns", "runtime_segments", "handoff", "mode", "metrics", "acceptance",
    }
)


def _scrub(value: Any) -> Any:
    """Recursively secret-scrub every string *and* mapping key.

    Tuples become lists (JSON has no tuples); anything else passes through
    untouched. Scrubbing keys too, because metric names, session ids, and
    unknown meta fields are all attacker-influenced strings in adversarial
    sessions.
    """
    if isinstance(value, str):
        cleaned, _ = redact_text(value)
        return cleaned
    if isinstance(value, dict):
        return {_scrub_key(key): _scrub(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_scrub(item) for item in value]
    return value


def _scrub_key(key: Any) -> Any:
    if isinstance(key, str):
        cleaned, _ = redact_text(key)
        return cleaned
    return key


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
            kind = _scrub_key(str(record.get("type", record.get("kind", "unknown"))))
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
    metrics_payload: dict[str, Any] = {}
    if metrics is not None:
        if isinstance(metrics, dict):
            metrics_payload = metrics
        elif hasattr(metrics, "to_dict"):
            try:
                metrics_payload = metrics.to_dict()
            except Exception:
                metrics_payload = {}
    else:
        metrics_path = root / "metrics.json"
        if metrics_path.is_file():
            try:
                loaded = json.loads(metrics_path.read_text(encoding="utf-8"))
                metrics_payload = loaded if isinstance(loaded, dict) else {}
            except (OSError, ValueError):
                metrics_payload = {}
    public_meta = {
        key: value for key, value in meta.items() if key in _PUBLIC_META_FIELDS
    }
    return {
        "session_id": _scrub(meta.get("session_id", root.name)),
        "meta": _scrub(public_meta),
        "lanes": lanes,
        "native_event_kinds": _tally(root / "events.jsonl"),
        "external_event_kinds": _tally(root / "acp-events.jsonl"),
        "metrics": _scrub(metrics_payload),
        "garuda_version": _scrub(garuda_version),
    }
