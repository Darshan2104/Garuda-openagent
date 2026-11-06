"""Bounded, read-only artifact completeness and availability projection.

Storage owners keep their existing write and validation contracts.  This module
defines the small descriptor they expose to readers and combines those owner
adapters for one already-authorized session.  A descriptor is evidence about a
specific representation; it never grants access to another session or path.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import uuid
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from garuda.core.session_records import (
    BoundedRecordReader,
    BoundedSessionRecords,
    RecordError,
    parse_json,
    parse_json_value,
)
from garuda.core.sessions import validate_session_ref

SCHEMA = "garuda.artifact-reference"
SCHEMA_VERSION = 1
MAX_REFERENCES = 128
MAX_PROJECTION_BYTES = 64 * 1024
MAX_DESCRIPTOR_BYTES = 8 * 1024
MAX_VERIFY_BYTES = 4 * 1024 * 1024
MAX_ARTIFACT_SOURCE_BYTES = 8 * 1024 * 1024
MAX_IDENTITY_CHARS = 512

COMPLETENESS = frozenset({"complete", "partial", "failed", "pending", "unknown"})
AVAILABILITY = frozenset({"available", "unavailable", "unknown"})
INTEGRITY = frozenset({"verified", "failed", "unverified", "unknown"})
SOURCE_KINDS = frozenset({
    "accepted_child_value", "background_tail", "context_archive", "external_summary",
    "flow_artifact", "legacy_buffer", "session_summary", "tool_observation",
})
CAPTURE_REASONS = frozenset({
    "background_log_missing", "capture_failed", "environment_and_tool_output_clipped",
    "environment_output_truncated", "inherited_incomplete_source",
    "post_tool_processing_changed", "running_or_tail_limited",
    "session_metadata_character_limit", "tool_output_option_clipped",
})
_SHA256 = re.compile(r"[0-9a-f]{64}")


class ArtifactReader(Protocol):
    """An existing storage owner adapted to the common read projection."""

    def artifact_references(
        self, *, limit: int = MAX_REFERENCES, byte_budget: int | None = None,
        record_reader: BoundedRecordReader | None = None,
        retained_descriptors: list[dict[str, Any]] | None = None,
        verification_bytes: int | None = None,
    ) -> list[dict[str, Any]]: ...


@dataclass(frozen=True)
class ArtifactProjection:
    references: list[dict[str, Any]]
    omitted_references: int = 0
    coverage: str = "complete"
    omission_count_status: str = "exact"

    def to_dict(self) -> dict[str, Any]:
        result = {
            "schema": "garuda.artifact-projection",
            "version": SCHEMA_VERSION,
            "references": [_public_descriptor(reference) for reference in self.references],
            "omitted_references": self.omitted_references,
            "omission_count_status": self.omission_count_status,
            "coverage": self.coverage,
            "limits": {
                "references": MAX_REFERENCES,
                "serialized_bytes": MAX_PROJECTION_BYTES,
                "verified_payload_bytes": MAX_VERIFY_BYTES,
            },
        }
        result["serialized_bytes"] = 0
        while result["serialized_bytes"] != len(_json_bytes(result)):
            result["serialized_bytes"] = len(_json_bytes(result))
        return result


def _json_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _identity(value: Any) -> str | None:
    return value if isinstance(value, str) and 0 < len(value) <= MAX_IDENTITY_CHARS else None



def _public_descriptor(reference: dict[str, Any]) -> dict[str, Any]:
    from garuda.core.failure_evidence import _safe_text

    result = deepcopy(reference)
    coverage = dict(result.get("field_coverage", {}))

    def clean(value: Any, path: str = "") -> Any:
        if isinstance(value, dict):
            return {key: item if key == "field_coverage" else clean(
                item, f"{path}.{key}" if path else key,
            ) for key, item in value.items()}
        if isinstance(value, str):
            sanitized, _ = _safe_text(value)
            if sanitized != value:
                coverage[path] = {"status": "withheld"}
                return "[withheld artifact metadata]"
        return value

    result = clean(result)
    if coverage:
        result["field_coverage"] = coverage
    checked = validate_descriptor(result)
    if checked is None:
        raise RecordError("public artifact descriptor exceeds its bounded metadata contract")
    return checked

def source_capture(
    *,
    kind: str,
    completeness: str,
    omitted_source_bytes: int | None = None,
    identity: str | None = None,
    media_type: str = "text/plain; charset=utf-8",
    redaction: str = "unknown",
    reason: str | None = None,
) -> dict[str, Any]:
    """Create bounded internal source facts for an owner publication attempt."""
    if (not isinstance(kind, str) or kind not in SOURCE_KINDS
            or not isinstance(completeness, str) or completeness not in COMPLETENESS):
        raise ValueError("invalid artifact source capture")
    if omitted_source_bytes is not None and (
        type(omitted_source_bytes) is not int or omitted_source_bytes < 0
    ):
        raise ValueError("invalid omitted byte count")
    result: dict[str, Any] = {
        "kind": kind,
        "completeness": completeness,
        "omitted_source_bytes": omitted_source_bytes,
        "media_type": media_type if _identity(media_type) else "application/octet-stream",
        "redaction": redaction if isinstance(redaction, str) and redaction in {"none", "applied", "unknown"} else "unknown",
    }
    if _identity(identity):
        result["identity"] = identity
    if isinstance(reason, str) and reason in CAPTURE_REASONS:
        result["reason"] = reason
    return result


def normalize_source_capture(
    value: Any, *, default_kind: str, supplied: bool = False,
) -> dict[str, Any]:
    """Allowlist owner-supplied facts; arbitrary tool metadata never persists."""
    if not isinstance(value, dict):
        return source_capture(
            kind=default_kind,
            completeness="complete" if value is None and not supplied else "unknown",
        )
    try:
        return source_capture(
            kind=value.get("kind", default_kind),
            completeness=value.get("completeness", "unknown"),
            omitted_source_bytes=value.get("omitted_source_bytes"),
            identity=value.get("identity"),
            media_type=value.get("media_type", "text/plain; charset=utf-8"),
            redaction=value.get("redaction", "unknown"),
            reason=value.get("reason"),
        )
    except ValueError:
        return source_capture(kind=default_kind, completeness="unknown")


def inherited_source_capture(
    descriptors: list[Any], *, kind: str, identity: str | None = None
) -> dict[str, Any]:
    """Carry the least-complete upstream fact into a later stored representation."""
    checked = [validate_descriptor(value) for value in descriptors]
    invalid = any(item is None for item in checked)
    valid = [item for item in checked if item is not None]
    if not valid and not invalid:
        return source_capture(kind=kind, completeness="complete", identity=identity)
    rank = {"failed": 0, "unknown": 1, "pending": 1, "partial": 2, "complete": 3}
    states, counts = (["unknown"], [None]) if invalid else ([], [])
    for item in valid:
        state = item["completeness"]
        omitted = item.get("omitted_source_bytes")
        view_loss = item.get("omitted_view_bytes")
        if state == "complete" and omitted is None:
            omitted = 0
        if item["owner"]["kind"] == "inline-tool-result":
            # The full source is not retained elsewhere: an archive can only
            # republish the already-shaped message, so its view loss is permanent.
            if view_loss is None:
                state, omitted = min((state, "unknown"), key=rank.__getitem__), None
            elif view_loss:
                if state == "complete":
                    state = "partial"
                omitted = omitted + view_loss if omitted is not None else None
        states.append(state)
        counts.append(omitted)
    completeness = min(states, key=rank.__getitem__)
    omitted = sum(counts) if counts and all(type(count) is int for count in counts) else None
    return source_capture(
        kind=kind,
        completeness=completeness,
        omitted_source_bytes=omitted,
        identity=identity,
        reason="inherited_incomplete_source" if completeness != "complete" else None,
    )


def descriptor(
    *,
    owner_kind: str,
    owner_reference: str,
    storage_session_id: str,
    producer_session_id: str | None,
    producer: str | None,
    source: dict[str, Any],
    size_bytes: int | None,
    sha256: str | None,
    size_provenance: str,
    digest_provenance: str,
    availability: str,
    integrity: str,
    reason: str | None = None,
    omitted_view_bytes: int | None = 0,
    retention: dict[str, Any] | None = None,
    representation: str = "stored-bytes",
) -> dict[str, Any]:
    """Build the versioned JSON read model used by every artifact consumer."""
    result: dict[str, Any] = {
        "schema": SCHEMA,
        "version": SCHEMA_VERSION,
        "owner": {"kind": owner_kind, "reference": owner_reference},
        "storage_session_id": storage_session_id,
        "producer_session_id": producer_session_id,
        "producer": producer,
        "media_type": source["media_type"],
        "source": {
            "kind": source["kind"],
            **({"identity": source["identity"]} if "identity" in source else {}),
        },
        "redaction": {"state": source["redaction"], "representation": representation},
        "completeness": source["completeness"],
        "omitted_source_bytes": source.get("omitted_source_bytes"),
        "omitted_view_bytes": omitted_view_bytes,
        "size": {"bytes": size_bytes, "provenance": size_provenance},
        "digest": {"sha256": sha256, "provenance": digest_provenance},
        "availability": {"state": availability, "integrity": integrity},
        "retention": retention or {"policy": "owner-defined"},
    }
    capture_reason = source.get("reason")
    if capture_reason:
        result["capture_reason"] = capture_reason
    if reason:
        result["availability"]["reason"] = reason
    if validate_descriptor(result) is None:
        raise ValueError("artifact descriptor exceeds or violates its contract")
    return result


def inline_descriptor(
    content: str,
    *,
    storage_session_id: str,
    producer: str,
    source: dict[str, Any],
    omitted_view_bytes: int | None = 0,
) -> dict[str, Any]:
    data = content.encode("utf-8")
    return descriptor(
        owner_kind="inline-tool-result",
        owner_reference=hashlib.sha256(_json_bytes({
            "digest": hashlib.sha256(data).hexdigest(), "session": storage_session_id,
            "producer": producer, "source": source, "omitted_view_bytes": omitted_view_bytes,
        })).hexdigest()[:24],
        storage_session_id=storage_session_id,
        producer_session_id=storage_session_id,
        producer=producer,
        source=source,
        size_bytes=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        size_provenance="observed",
        digest_provenance="observed",
        availability="available",
        integrity="verified",
        omitted_view_bytes=omitted_view_bytes,
        retention={"policy": "session-transcript"},
        representation="delivered-tool-result",
    )


def failed_descriptor(
    *, storage_session_id: str, producer: str, source: dict[str, Any], reason: str
) -> dict[str, Any]:
    failed_source = {**source, "completeness": "failed", "reason": "capture_failed",
                     "omitted_source_bytes": None}
    return descriptor(
        owner_kind="capture-attempt",
        owner_reference=uuid.uuid4().hex,
        storage_session_id=storage_session_id,
        producer_session_id=storage_session_id,
        producer=producer,
        source=failed_source,
        size_bytes=None,
        sha256=None,
        size_provenance="unknown",
        digest_provenance="unknown",
        availability="unavailable",
        integrity="unknown",
        reason="capture_failed",
        omitted_view_bytes=None,
        retention={"policy": "capture-attempt-record"},
    )


def validate_descriptor(value: Any) -> dict[str, Any] | None:
    """Validate the only artifact metadata contract retained in messages/events."""
    if not isinstance(value, dict) or value.get("schema") != SCHEMA:
        return None
    if type(value.get("version")) is not int or value["version"] != SCHEMA_VERSION:
        return None
    required = {
        "schema", "version", "owner", "storage_session_id", "producer_session_id",
        "producer", "media_type", "source", "redaction", "completeness",
        "omitted_source_bytes", "omitted_view_bytes", "size", "digest",
        "availability", "retention",
    }
    if not required <= value.keys() or value.keys() - required - {"capture_reason", "field_coverage"}:
        return None
    field_coverage = value.get("field_coverage", {})
    if not isinstance(field_coverage, dict) or len(field_coverage) > 32:
        return None
    scalar_paths = {
        key for key, item in value.items() if not isinstance(item, dict)
    } | {
        f"{key}.{field}" for key, item in value.items()
        if isinstance(item, dict) and key != "field_coverage" for field in item
    }
    for path, coverage in field_coverage.items():
        if (not isinstance(path, str) or len(path) > 128
                or not re.fullmatch(r"[a-z_]+(?:\.[a-z_]+){0,3}", path)
                or path not in scalar_paths or coverage != {"status": "withheld"}):
            return None
    owner, source, redaction = value.get("owner"), value.get("source"), value.get("redaction")
    size, digest, availability = value.get("size"), value.get("digest"), value.get("availability")
    retention = value.get("retention")
    if not all(isinstance(item, dict) for item in (
        owner, source, redaction, size, digest, availability, retention
    )):
        return None
    if (
        set(owner) != {"kind", "reference"}
        or set(source) - {"kind", "identity"}
        or set(redaction) != {"state", "representation"}
        or set(size) != {"bytes", "provenance"}
        or set(digest) != {"sha256", "provenance"}
        or set(availability) - {"state", "integrity", "reason"}
        or set(retention) - {"policy", "seconds", "guaranteed_until"}
        or _identity(retention.get("policy")) is None
    ):
        return None
    if "seconds" in retention and (
        type(retention["seconds"]) is not int or retention["seconds"] < 0
    ):
        return None
    if "guaranteed_until" in retention and type(retention["guaranteed_until"]) is not bool:
        return None
    for item in (
        owner.get("kind"), owner.get("reference"), value.get("storage_session_id"),
        value.get("media_type"), source.get("kind"), redaction.get("state"),
        redaction.get("representation"), size.get("provenance"),
        digest.get("provenance"), availability.get("state"), availability.get("integrity"),
    ):
        if _identity(item) is None:
            return None
    for optional in (
        value.get("producer_session_id"), value.get("producer"), source.get("identity"),
        value.get("capture_reason"), availability.get("reason"),
    ):
        if optional is not None and _identity(optional) is None:
            return None
    if not isinstance(value.get("completeness"), str) or value["completeness"] not in COMPLETENESS:
        return None
    if redaction.get("state") not in {"none", "applied", "unknown"}:
        return None
    if size.get("provenance") not in {"recorded", "observed", "unknown"}:
        return None
    if digest.get("provenance") not in {"recorded", "observed", "unknown"}:
        return None
    if availability.get("state") not in AVAILABILITY or availability.get("integrity") not in INTEGRITY:
        return None
    for count in (value.get("omitted_source_bytes"), value.get("omitted_view_bytes"), size.get("bytes")):
        if count is not None and (type(count) is not int or count < 0):
            return None
    sha = digest.get("sha256")
    if sha is not None and (not isinstance(sha, str) or not _SHA256.fullmatch(sha)):
        return None
    try:
        encoded = _json_bytes(value)
    except (TypeError, ValueError, RecursionError):
        return None
    return deepcopy(value) if len(encoded) <= MAX_DESCRIPTOR_BYTES else None


def capture_commitments_disagree(left: dict, right: dict) -> bool:
    """Compare already-validated capture identities and known byte commitments."""
    if left["owner"] != right["owner"] or left["storage_session_id"] != right["storage_session_id"]:
        return True
    for key in ("producer_session_id", "producer"):
        if left[key] is not None and right[key] is not None and left[key] != right[key]:
            return True
    return any(left[field][key] is not None and right[field][key] is not None
               and left[field][key] != right[field][key]
               for field, key in (("size", "bytes"), ("digest", "sha256")))


def merge_capture_commitments(previous: dict, current: dict) -> dict:
    """Carry known commitments through compatible, already-validated records."""
    if capture_commitments_disagree(previous, current):
        raise ValueError("capture commitments conflict")
    merged = deepcopy(current)
    for key in ("producer_session_id", "producer"):
        if previous[key] is not None:
            merged[key] = previous[key]
    for field, key in (("size", "bytes"), ("digest", "sha256")):
        if previous[field][key] is not None:
            # Keep the original commitment's provenance, not a fresh observation.
            merged[field] = deepcopy(previous[field])
    validated = validate_descriptor(merged)
    if validated is None:
        raise ValueError("merged capture commitments exceed their descriptor contract")
    return validated


def artifact_inheritance_record(source_session: str, target_session: str, references: list[dict], coverage: str) -> dict:
    """Bound a resume owner's retained facts without granting source-session access."""
    value = {
        "version": 1, "source_session_id": validate_session_ref(source_session),
        "storage_session_id": validate_session_ref(target_session),
        "coverage": "complete" if coverage == "complete" else "unknown", "references": [],
    }
    for reference in references:
        capture = validate_descriptor(reference)
        if capture is None or capture["owner"]["kind"] not in {"buffer", "inline-tool-result", "capture-attempt"}:
            value["coverage"] = "unknown"
            continue
        capture["storage_session_id"] = target_session
        value["references"].append(capture)
        if len(value["references"]) > MAX_REFERENCES or len(_json_bytes(value)) > MAX_PROJECTION_BYTES:
            value["references"].pop()
            value["coverage"] = "unknown"
            break
    return value


def validate_artifact_inheritance(value: Any, session_id: str) -> dict | None:
    if not isinstance(value, dict) or set(value) != {
        "version", "source_session_id", "storage_session_id", "coverage", "references",
    }:
        return None
    if (type(value["version"]) is not int or value["version"] != 1
            or value["storage_session_id"] != session_id
            or not isinstance(value["coverage"], str) or value["coverage"] not in {"complete", "unknown"}
            or not isinstance(value["references"], list) or len(value["references"]) > MAX_REFERENCES):
        return None
    try:
        validate_session_ref(value["source_session_id"])
        if value["source_session_id"] == session_id or len(_json_bytes(value)) > MAX_PROJECTION_BYTES:
            return None
        for reference in value["references"]:
            checked = validate_descriptor(reference)
            if (checked is None or checked["storage_session_id"] != session_id
                    or checked["owner"]["kind"] not in {"buffer", "inline-tool-result", "capture-attempt"}):
                return None
        return deepcopy(value)
    except (TypeError, ValueError, RecursionError):
        return None


def retained_capture_descriptors(
    records, session_id: str, *, verification_bytes: int
) -> tuple[list[dict[str, Any]], bool, int]:
    """Read the allowlisted message/event contract; never inspect raw tool metadata."""
    found: list[dict[str, Any]] = []
    unknown = False
    verification_remaining = verification_bytes

    def retain(metadata, content):
        nonlocal unknown, verification_remaining
        if not isinstance(metadata, dict):
            unknown = unknown or metadata is not None
            return
        capture = validate_descriptor(metadata.get("artifact_capture"))
        if "artifact_capture" in metadata:
            if capture is None or capture["storage_session_id"] != session_id:
                unknown = True
            else:
                kind = capture["owner"]["kind"]
                if kind == "inline-tool-result":
                    expected_size, expected_digest = capture["size"]["bytes"], capture["digest"]["sha256"]
                    if capture["redaction"]["representation"] == "withheld-tool-result":
                        capture["availability"] = {"state": "unavailable", "integrity": "unknown",
                                                   "reason": "delivery_interrupted"}
                    elif capture["redaction"]["representation"] == "pruned-tool-result":
                        capture["availability"] = {"state": "unavailable", "integrity": "unknown",
                                                   "reason": "inline_payload_pruned"}
                    elif not isinstance(content, str):
                        capture["availability"] = {"state": "unavailable", "integrity": "unknown",
                                                   "reason": "inline_payload_missing"}
                    elif expected_size is None or expected_digest is None:
                        capture["availability"] = {"state": "unknown", "integrity": "unverified",
                                                   "reason": "inline_commitment_unknown"}
                    else:
                        data = content.encode("utf-8")
                        if len(data) > verification_remaining:
                            capture["availability"] = {"state": "unknown", "integrity": "unverified",
                                                       "reason": "verification_budget_exhausted"}
                        else:
                            verification_remaining -= len(data)
                            matches = len(data) == expected_size and hashlib.sha256(data).hexdigest() == expected_digest
                            capture["availability"] = {
                                "state": "available" if matches else "unavailable",
                                "integrity": "verified" if matches else "failed",
                                **({} if matches else {"reason": "inline_payload_mismatch"}),
                            }
                elif kind == "capture-attempt":
                    capture["availability"] = {"state": "unavailable", "integrity": "unknown",
                                               "reason": capture["availability"].get("reason") or "capture_failed"}
                    capture["completeness"] = "failed"
                elif kind != "buffer":
                    # Flow and summary records have their own adapters; a
                    # transcript cannot grant or attest those owners' payloads.
                    unknown = True
                    capture = None
                if capture is not None:
                    found.append(capture)
        from garuda.core.child_result import (
            artifact_from_child_result_reference,
            failed_child_publication_descriptor,
            validate_child_result_envelope,
        )

        def reserve_child_verification(size: int) -> bool:
            nonlocal verification_remaining
            if size > verification_remaining:
                return False
            # The child owner may reject the digest/schema after hashing. The
            # attempt still spent these bytes and cannot refund verification.
            verification_remaining -= size
            return True

        child = validate_child_result_envelope(
            metadata.get("child_result"), reserve_verification=reserve_child_verification,
        )
        if "child_result" in metadata and child is None:
            unknown = True
        if child is not None and child["delivery"]["state"] == "referenced":
            reference = child["delivery"]["reference"]
            capture = artifact_from_child_result_reference(reference)
            if reference["parent_session_id"] != session_id:
                unknown = True
            elif capture is not None:
                found.append(capture)
        elif child is not None:
            failure = failed_child_publication_descriptor(child, session_id)
            if failure is not None:
                found.append(failure)

    inheritance_path = Path(records.root) / session_id / "artifact-inheritance.json"
    try:
        info = os.lstat(inheritance_path)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise RecordError("artifact inheritance is not a regular file")
        inherited = validate_artifact_inheritance(parse_json(records.read(
            Path(session_id) / "artifact-inheritance.json", limit=MAX_PROJECTION_BYTES,
        )), session_id)
        if inherited is None:
            raise RecordError("artifact inheritance has an invalid contract")
        unknown = unknown or inherited["coverage"] != "complete"
        for capture in inherited["references"]:
            if capture["owner"]["kind"] == "inline-tool-result":
                capture["availability"] = {"state": "unavailable", "integrity": "unknown",
                                           "reason": "inherited_inline_payload_not_retained"}
            elif capture["owner"]["kind"] == "capture-attempt":
                capture["availability"] = {"state": "unavailable", "integrity": "unknown",
                                           "reason": capture["availability"].get("reason") or "capture_failed"}
                capture["completeness"] = "failed"
            found.append(capture)
    except FileNotFoundError:
        try:
            # The shared reader owns discovery uncertainty for both inspection
            # and migration. A later resume must not erase a lost generation.
            selected_meta = records.load_meta(session_id)
            if selected_meta.get("session_id") != session_id:
                raise RecordError("selected session metadata has a conflicting identity")
            unknown = unknown or "resumed_from" in selected_meta
        except (OSError, RecordError, TypeError, ValueError, KeyError, RecursionError):
            unknown = True
    except (OSError, RecordError, TypeError, ValueError, KeyError, RecursionError):
        unknown = True

    messages_path = Path(records.root) / session_id / "messages.json"
    try:
        info = os.lstat(messages_path)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise OSError("messages record is not a regular file")
        raw = records.read(Path(session_id) / "messages.json", limit=4 * 1024 * 1024)
        messages = parse_json_value(raw)
        if not isinstance(messages, list):
            raise ValueError("messages are not a list")
        for index, message in enumerate(messages):
            if index >= 4096:
                unknown = True
                break
            if not isinstance(message, dict):
                unknown = True
                continue
            try:
                retain(message.get("metadata"), message.get("content"))
            except (RecordError, ValueError, TypeError, KeyError, UnicodeError, RecursionError):
                unknown = True
    except (RecordError, OSError, ValueError, TypeError, UnicodeError, RecursionError, KeyError):
        unknown = unknown or messages_path.exists() or messages_path.is_symlink()
    # Active and failed-before-finish sessions may have no messages.json. Events
    # carry the same allowlisted descriptor and make the state visible meanwhile.
    events_path = Path(records.root) / session_id / "events.jsonl"
    try:
        info = os.lstat(events_path)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise OSError("event record is not a regular file")
        raw = records.read(Path(session_id) / "events.jsonl", limit=4 * 1024 * 1024)
        lines = raw.splitlines()
        for index, line in enumerate(lines):
            if index >= 4096:
                unknown = True
                break
            try:
                event = parse_json(line)
                kind = event.get("type")
                if not isinstance(kind, str):
                    unknown = True
                elif kind == "tool_result":
                    payload = event.get("payload")
                    if not isinstance(payload, dict):
                        unknown = True
                        continue
                    retain(payload, payload.get("content"))
            except (RecordError, ValueError, TypeError, KeyError, UnicodeError, RecursionError):
                unknown = True
    except (RecordError, OSError, ValueError, TypeError, UnicodeError, RecursionError, KeyError):
        unknown = unknown or events_path.exists() or events_path.is_symlink()
    unique: dict[tuple[str, str], dict[str, Any]] = {}
    for capture in found:
        key = (capture["owner"]["kind"], capture["owner"]["reference"])
        previous = unique.get(key)
        if previous is not None and key[0] == "buffer" and (
            previous["availability"].get("reason") == "capture_commitment_conflict"
            or capture_commitments_disagree(previous, capture)
        ):
            previous["availability"] = {"state": "unavailable", "integrity": "failed",
                                        "reason": "capture_commitment_conflict"}
            unknown = True
            continue
        if previous is not None and key[0] == "buffer":
            try:
                capture = merge_capture_commitments(previous, capture)
            except ValueError:
                previous["availability"] = {"state": "unavailable", "integrity": "failed",
                                            "reason": "capture_commitment_conflict"}
                unknown = True
                continue
        unique[key] = capture
    return list(unique.values()), unknown, verification_bytes - verification_remaining


def project_session_artifacts(
    store,
    session_id: str,
    *,
    expected_project_id: str | None = None,
    limit: int = MAX_REFERENCES,
) -> dict[str, Any]:
    """Project one selected session without deriving authority from a reference."""
    session_id = validate_session_ref(session_id)
    if type(limit) is not int or not 1 <= limit <= MAX_REFERENCES:
        raise ValueError("artifact projection limit is outside its bounded range")
    records = store if isinstance(store, BoundedSessionRecords) else BoundedSessionRecords(
        store.root, byte_budget=MAX_ARTIFACT_SOURCE_BYTES
    )
    with records.selected_session(session_id):
        return _project_selected_artifacts(records, session_id, expected_project_id, limit)


def _project_selected_artifacts(records, session_id, expected_project_id, limit):
    source_budget = records.remaining
    meta = records.load_meta(session_id)
    if meta.get("session_id") != session_id:
        raise ValueError("session identity conflicts with its selected directory")
    if expected_project_id is not None and meta.get("project_id") != expected_project_id:
        raise ValueError("session belongs to a different project")

    from garuda.core.buffer import ToolOutputBuffer

    owner = ToolOutputBuffer(
        session_id=session_id,
        root=Path(records.root) / session_id / "buffers",
    )
    references: list[dict[str, Any]] = []
    summary_verified_bytes = 0
    summary_identity_unknown = False
    final_message = meta.get("final_message")
    if isinstance(final_message, str):
        data = final_message.encode("utf-8")
        summary_verified_bytes = len(data)
        segments = meta.get("runtime_segments") if isinstance(meta.get("runtime_segments"), list) else []
        segment = segments[-1] if segments and isinstance(segments[-1], dict) else {}
        external = segment.get("kind") == "acp"
        runtime = _identity(segment.get("runtime_id"))
        summary_identity_unknown = "runtime_id" in segment and runtime is None
        summary_source = source_capture(
            kind="external_summary" if external else "session_summary",
            completeness="complete" if len(final_message) < 2000 else "unknown",
            identity=runtime,
            reason="session_metadata_character_limit" if len(final_message) >= 2000 else None,
        )
        summary_reference = descriptor(
            owner_kind="session-summary", owner_reference="meta.json:final_message",
            storage_session_id=session_id, producer_session_id=session_id,
            producer=runtime,
            source=summary_source, size_bytes=len(data), sha256=hashlib.sha256(data).hexdigest(),
            size_provenance="observed", digest_provenance="observed",
            availability="available", integrity="verified",
            retention={"policy": "session-metadata"}, representation="stored-summary",
        )
        if summary_identity_unknown:
            summary_reference["field_coverage"] = {"producer": {"status": "withheld"}}
        references.append(summary_reference)
    persisted, persisted_unknown, inline_verified_bytes = retained_capture_descriptors(
        records, session_id, verification_bytes=max(0, MAX_VERIFY_BYTES - summary_verified_bytes),
    )
    buffer_slots = max(0, limit - len(references))
    buffer_references = owner.artifact_references(
        limit=max(1, buffer_slots), record_reader=records, retained_descriptors=persisted,
        verification_bytes=max(0, MAX_VERIFY_BYTES - summary_verified_bytes - inline_verified_bytes),
    )
    references.extend(buffer_references[:buffer_slots])
    omitted = max(0, owner.reference_count - buffer_slots)
    known = owner.reference_keys | {
        (item["owner"]["kind"], item["owner"]["reference"]) for item in references
    }
    for capture in persisted:
        key = (capture["owner"]["kind"], capture["owner"]["reference"])
        if key in known:
            continue
        if len(references) >= limit:
            omitted += 1
            continue
        references.append(capture)
        known.add(key)
    coverage = "unknown" if persisted_unknown or summary_identity_unknown else owner.reference_coverage
    if meta.get("kind") == "flow":
        from garuda.core import starter_evidence

        declared = starter_evidence.flow_steps(meta)
        if declared is None:
            # Invalid declarations cannot authorize receipt adaptation. Keep
            # already-collected owners and expose the missing flow coverage.
            receipts, receipt_coverage = [], {"journal": "unknown"}
        else:
            receipts, receipt_coverage = starter_evidence.flow_records(records, session_id, declared)
        flow_total = 0
        verification_remaining = max(0, MAX_VERIFY_BYTES - summary_verified_bytes - inline_verified_bytes - owner.payload_read_bytes)
        for receipt in receipts:
            for record in receipt.get("outputs") or []:
                try:
                    starter_evidence.artifact_reference(receipt, record)
                except (RecordError, ValueError, TypeError, KeyError):
                    coverage = "unknown"
                    continue
                flow_total += 1
                if len(references) >= limit:
                    continue
                before_read = records.remaining
                projected, _valid = starter_evidence.artifact(
                    records, session_id, receipt, record,
                    verification_bytes=verification_remaining,
                )
                verification_remaining = max(
                    0, verification_remaining - (before_read - records.remaining),
                )
                common = projected.get("artifact")
                if common is not None and len(references) < limit:
                    references.append(common)
        omitted += max(0, flow_total - sum(
            1 for reference in references if reference["owner"]["kind"] == "flow-artifact"
        ))
        if (receipt_coverage.get("journal") == "unknown"
                or receipt_coverage.get("unreadable_records")
                or receipt_coverage.get("missing_records")):
            coverage = "unknown"
    if omitted:
        coverage = "clipped"
    projection = ArtifactProjection(
        references, omitted, coverage,
        "lower-bound" if coverage != "complete" else "exact",
    ).to_dict()
    projection["limits"]["source_bytes"] = source_budget
    while len(_json_bytes(projection)) > MAX_PROJECTION_BYTES and projection["references"]:
        projection["references"].pop()
        projection["omitted_references"] += 1
        projection["coverage"] = "clipped"
    projection["serialized_bytes"] = 0
    while projection["serialized_bytes"] != len(_json_bytes(projection)):
        projection["serialized_bytes"] = len(_json_bytes(projection))
    if len(_json_bytes(projection)) > MAX_PROJECTION_BYTES:
        raise RecordError("artifact projection exceeds its serialized byte budget")
    return projection
