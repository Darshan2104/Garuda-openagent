"""Bounded, redacted support summaries over selected session records."""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from garuda.core.failure_evidence import (
    MAX_AGGREGATE_SOURCE_BYTES,
    SessionFailureEvidence,
    _jsonl,
    _safe_text,
    unavailable_projection,
)
from garuda.core.session_records import BoundedSessionRecords, RecordError, parse_json

MAX_BUNDLE_BYTES = 1024 * 1024
_PUBLIC_META_FIELDS = frozenset({
    "session_id", "schema_version", "status", "created_at", "updated_at",
    "turns", "runtime_segments", "handoff", "mode", "metrics", "acceptance",
})


def _bounded_scrub(value: Any, depth: int = 0) -> tuple[Any, bool]:
    if depth >= 6:
        return "[withheld: structure limit]", True
    if isinstance(value, str):
        return _safe_text(value)
    if isinstance(value, dict):
        out = {}
        clipped = len(value) > 128
        for index, (key, item) in enumerate(value.items()):
            if index >= 128:
                break
            safe_key, key_clipped = _safe_text(str(key))
            if key_clipped:
                out["[withheld diagnostic subtree]"] = None
                clipped = True
                continue
            safe, item_clipped = _bounded_scrub(item, depth + 1)
            out[safe_key] = safe
            clipped = clipped or key_clipped or item_clipped
        return out, clipped
    if isinstance(value, (list, tuple)):
        items = [_bounded_scrub(item, depth + 1) for item in value[:128]]
        return [item for item, _ in items], len(value) > 128 or any(flag for _, flag in items)
    if isinstance(value, float) and not math.isfinite(value):
        return None, True
    if value is None or isinstance(value, (int, float, bool)):
        return value, False
    return None, True


def _scrub(value: Any) -> Any:
    return _bounded_scrub(value)[0]


def _tally(records: list[dict], coverage: dict) -> tuple[dict[str, int], dict]:
    kinds: dict[str, int] = {}
    clipped = False
    for record in records:
        raw = record.get("type", record.get("kind", "unknown"))
        kind, limited = _safe_text(raw if isinstance(raw, str) else "malformed")
        clipped = clipped or limited
        if len(kinds) >= 128 and kind not in kinds:
            clipped = True
            continue
        kinds[kind] = kinds.get(kind, 0) + 1
    if clipped:
        coverage = {**coverage, "status": "clipped", "reason": "event-kind summary limit reached"}
    return kinds, coverage


def build_support_bundle(session_dir: str | Path, *, metrics: dict | None = None,
                         garuda_version: str = "unknown") -> dict[str, Any]:
    """Read bounded no-follow sources; unavailable coverage never becomes a zero claim."""
    root = Path(session_dir)
    records = BoundedSessionRecords(root.parent, byte_budget=MAX_AGGREGATE_SOURCE_BYTES)
    try:
        meta = records.load_meta(root.name)
    except (RecordError, ValueError) as exc:
        raise ValueError("selected support metadata is unavailable") from exc
    if meta.get("session_id") != root.name:
        raise ValueError("selected support session identity conflicts with its directory")
    native, native_coverage = _jsonl(records, Path(root.name) / "events.jsonl", limit=4 * 1024 * 1024)
    external, external_coverage = _jsonl(records, Path(root.name) / "acp-events.jsonl", limit=4 * 1024 * 1024)
    native_kinds, native_coverage = _tally(native, native_coverage)
    external_kinds, external_coverage = _tally(external, external_coverage)
    lanes = []
    lane_coverage = {"status": "unavailable", "reason": "lane count sources are unavailable"}
    if native_coverage["status"] != "unavailable" and external_coverage["status"] != "unavailable":
        try:
            from garuda.observability.lanes import build_cross_runtime, export_trace

            selected_meta, _ = _bounded_scrub(meta)
            segments = selected_meta.get("runtime_segments")
            if isinstance(segments, list):
                selected_meta["runtime_segments"] = segments[:32]
            lanes = export_trace(build_cross_runtime(
                selected_meta, len(native), external, fallback_session_id=root.name,
            ))["lanes"]
            complete = native_coverage["status"] == external_coverage["status"] == "present"
            lane_coverage = {"status": "present" if complete else "clipped", "source": "selected records"}
        except (TypeError, ValueError, AttributeError):
            lane_coverage = {"status": "unavailable", "reason": "unsupported lane metadata"}
    metrics_payload = {}
    metrics_coverage = {"status": "present", "source": "caller"}
    if metrics is not None:
        try:
            metrics_payload = metrics if isinstance(metrics, dict) else metrics.to_dict()
        except (AttributeError, TypeError, ValueError):
            metrics_coverage = {"status": "unavailable", "source": "caller"}
    else:
        try:
            metrics_payload = parse_json(records.read(Path(root.name) / "metrics.json"))
            metrics_coverage["source"] = "metrics.json"
        except (RecordError, ValueError):
            metrics_coverage = {"status": "unavailable", "source": "metrics.json"}
    metrics_payload, clipped_metrics = _bounded_scrub(metrics_payload)
    public_meta, clipped_meta = _bounded_scrub({key: value for key, value in meta.items()
                                             if key in _PUBLIC_META_FIELDS})
    if clipped_metrics:
        metrics_coverage["status"] = "clipped"
    try:
        failure = SessionFailureEvidence(records).project(root.name)
    except ValueError as exc:
        failure = unavailable_projection(str(exc))
    bundle = {
        "session_id": _scrub(root.name), "meta": public_meta, "lanes": _scrub(lanes),
        "native_event_kinds": native_kinds, "external_event_kinds": external_kinds,
        "metrics": metrics_payload, "garuda_version": _scrub(garuda_version),
        "failure_evidence": failure,
        "coverage": {
            "meta": {"status": "clipped" if clipped_meta else "present", "source": "meta.json"},
            "native_events": native_coverage, "external_events": external_coverage,
            "lanes": lane_coverage, "metrics": metrics_coverage,
        },
        "limits": {"bundle_bytes": MAX_BUNDLE_BYTES,
                   "summary_source_bytes": MAX_AGGREGATE_SOURCE_BYTES,
                   "failure_source_bytes": MAX_AGGREGATE_SOURCE_BYTES},
    }
    for field in ("metrics", "meta", "lanes"):
        if len(json.dumps(bundle, ensure_ascii=False).encode("utf-8")) <= MAX_BUNDLE_BYTES:
            break
        bundle[field] = [] if field == "lanes" else {}
        bundle["coverage"][field] = {"status": "clipped", "reason": "support bundle byte limit reached"}
    if len(json.dumps(bundle, ensure_ascii=False).encode("utf-8")) > MAX_BUNDLE_BYTES:
        raise ValueError("support bundle exceeds its serialized byte limit")
    return bundle
