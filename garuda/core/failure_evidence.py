"""Bounded, read-only failure evidence and explicit diagnostic snapshots.

The projection is deliberately narrower than a transcript.  Writers retain a
small allowlisted causal envelope; readers combine it with facts already owned
by session metadata and bounded event records.  Inspection never discovers a
runtime, probes a provider, launches work, or opens a referenced workspace.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import stat
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from garuda.context.redact import redact_text
from garuda.core.session_records import (
    BoundedSessionRecords,
    RecordError,
    list_json,
    parse_json,
    read_bytes,
    session_reference_digest,
)
from garuda.core.sessions import validate_session_ref

SCHEMA = "garuda.failure-evidence"
SCHEMA_VERSION = 1
CAUSE_FILE = "failure-causes-v1.jsonl"
DIAGNOSTIC_DIR = "diagnostics-v1"
MAX_PROJECTION_BYTES = 256 * 1024
MAX_TEXT_BYTES = 2 * 1024
MAX_SANITIZE_BYTES = 64 * 1024
MAX_CAUSES = 128
MAX_CAUSE_FILE_BYTES = 256 * 1024
MAX_SOURCE_BYTES = 4 * 1024 * 1024
MAX_AGGREGATE_SOURCE_BYTES = 16 * 1024 * 1024
MAX_LINE_BYTES = 64 * 1024
MAX_CAUSAL_RECORD_BYTES = 4 * 1024
MAX_RECORDS = 4096
MAX_RECEIPT_BYTES = 512 * 1024
MAX_SNAPSHOT_ENTRIES = 64
MAX_SNAPSHOTS = 8
MAX_SNAPSHOT_BYTES = 2 * 1024 * 1024
SNAPSHOT_RETENTION_SECONDS = 7 * 24 * 60 * 60

_CATEGORIES = frozenset(
    {"causes", "identities", "workspace", "approvals", "review", "acceptance"}
)
_C0_C1 = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")
_ANSI = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\))")
_REASONING = re.compile(r"(?i)(<thinking>|<reasoning>|chain[- ]of[- ]thought)")
_CAUSE_KINDS = frozenset({"execution_failure", "recovery_refusal", "cleanup_uncertainty"})


def _reasoning_text(text: str) -> bool:
    # Local protocol exceptions can contain repr-escaped ANSI controls.
    unescaped = re.sub(r"\\(?:x1[bB]|u001[bB])", "\x1b", text)
    return bool(_REASONING.search(_ANSI.sub("", unescaped)))


def _preview_digest(reference_digest: str, categories: tuple | list, serialized: str) -> str:
    manifest = {"session_ref_digest": reference_digest, "categories": sorted(categories),
                "json": serialized}
    return hashlib.sha256(json.dumps(manifest, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")).encode("utf-8")).hexdigest()




def _read(store, relative: Path, *, limit: int) -> bytes:
    return store.read(relative, limit) if isinstance(store, BoundedSessionRecords) else read_bytes(
        store, relative, limit=limit
    )


def _read_json(store, relative: Path) -> dict[str, Any]:
    return parse_json(_read(store, relative, limit=MAX_SOURCE_BYTES))


class FailureEvidenceError(ValueError):
    """A diagnostic read or explicit snapshot operation was refused."""


class FailureEvidenceReader(Protocol):
    def project(
        self, session_id: str, *, expected_project_id: str | None = None
    ) -> dict[str, Any]: ...


def unavailable_projection(reason: str) -> dict[str, Any]:
    """A stable failure-evidence shape for a containing session view."""
    cleaned, _ = _safe_text(reason)
    return {
        "schema": SCHEMA,
        "version": SCHEMA_VERSION,
        "original_cause": _field(
            "unavailable", reason="bounded failure evidence could not be read"
        ),
        "coverage": {
            "projection": _field("unavailable", reason=cleaned, source="session-store")
        },
    }


def _field(
    status: str,
    value: Any = None,
    *,
    reason: str | None = None,
    source: str | None = None,
) -> dict[str, Any]:
    out: dict[str, Any] = {"status": status}
    if status in {"present", "clipped"} and value is not None:
        out["value"] = value
    if reason:
        out["reason"] = reason
    if source:
        cleaned_source, clipped_source = _safe_text(source)
        out["source"] = cleaned_source
        if cleaned_source != source:
            out["source_status"] = "clipped" if clipped_source else "withheld"
    return out


def _safe_text(value: object) -> tuple[str, bool]:
    """Redact, neutralize terminal controls, then clip on a UTF-8 boundary."""
    text = value if isinstance(value, str) else str(value)
    if len(text.encode("utf-8")) > MAX_SANITIZE_BYTES or _reasoning_text(text):
        return "[withheld diagnostic text]", True
    cleaned = _ANSI.sub("", text).replace("\r", "\n")
    cleaned = _C0_C1.sub("�", cleaned)
    cleaned, _ = redact_text(cleaned)
    encoded = cleaned.encode("utf-8")
    if len(encoded) <= MAX_TEXT_BYTES:
        return cleaned, False
    clipped = encoded[:MAX_TEXT_BYTES].decode("utf-8", "ignore")
    return clipped, True


def _safe_scalar(value: object, *, limit: int = 256) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    cleaned, _ = _safe_text(value)
    encoded = cleaned.encode("utf-8")
    return encoded[:limit].decode("utf-8", "ignore")


def identity_evidence(value: Any, source: str | None = None, *, limit: int = 256) -> dict[str, Any]:
    if not isinstance(value, str) or not value:
        return _field("unavailable", reason="identity was not recorded", source=source)
    if _reasoning_text(value) or len(value.encode("utf-8")) > MAX_SANITIZE_BYTES:
        return _field("withheld", reason="unsafe or oversized identity text", source=source)
    cleaned, clipped = _safe_text(value)
    encoded = cleaned.encode("utf-8")
    clipped = clipped or len(encoded) > limit
    return _field("clipped" if clipped else "present", encoded[:limit].decode("utf-8", "ignore"),
                  reason="identity text exceeded its byte limit" if clipped else None, source=source)

def _detail_field(value: object, source: str, *, already_clipped: bool = False) -> dict[str, Any]:
    if not isinstance(value, str):
        return _field("withheld", reason="unsupported explanatory value shape", source=source)
    text = value
    if len(text.encode("utf-8")) > MAX_SANITIZE_BYTES or _reasoning_text(text):
        return _field("withheld", reason="unsafe or oversized explanatory text", source=source)
    detail, clipped = _safe_text(text)
    return _field("clipped" if clipped or already_clipped else "present", detail, source=source)


def classify_exception(exc: BaseException) -> tuple[str, str]:
    """Return a stable code and boundary without classifying arbitrary prose."""
    from garuda.acp.protocol import (
        AcpCancelledError,
        AcpExitError,
        AcpProtocolError,
        AcpRemoteError,
        AcpTimeoutError,
    )

    if isinstance(exc, AcpRemoteError):
        suffix = "internal_error" if exc.remote_code == -32603 else "reported_error"
        return f"acp.remote.{suffix}", "agent-response"
    if isinstance(exc, AcpTimeoutError):
        return "acp.timeout", "agent-transport"
    if isinstance(exc, AcpExitError):
        return "acp.process_exit", "agent-process"
    if isinstance(exc, AcpCancelledError):
        return "acp.cancelled", "agent-runtime"
    if isinstance(exc, AcpProtocolError):
        if "version mismatch" in str(exc).lower():
            return "acp.protocol.version_mismatch", "protocol-negotiation"
        return "acp.protocol.malformed", "protocol"
    if isinstance(exc, PermissionError):
        return "permission.denied", "permission-broker"
    return "runtime.unknown", "runtime"


def causal_record(
    exc: BaseException,
    *,
    phase: str,
    runtime_id: str | None = None,
    adapter_version: str | None = None,
    reported_agent: str | None = None,
    protocol_version: int | None = None,
    kind: str = "execution_failure",
    code: str | None = None,
    boundary: str | None = None,
    linked_to: str | None = None,
) -> dict[str, Any]:
    inferred_code, inferred_boundary = classify_exception(exc)
    raw_detail = str(exc)
    oversized = len(raw_detail.encode("utf-8")) > MAX_SANITIZE_BYTES
    detail, clipped = ("", False) if oversized else _safe_text(raw_detail)
    withheld = oversized or _reasoning_text(raw_detail)
    record = {
        "schema": SCHEMA,
        "version": SCHEMA_VERSION,
        "recorded_at": time.time(),
        "kind": kind,
        "code": code or inferred_code,
        "phase": _safe_scalar(phase, limit=64) or "unknown",
        "boundary": boundary or inferred_boundary,
        "detail": (
            {"status": "withheld", "reason": (
                "explanatory text exceeded the sanitization budget" if oversized
                else "reasoning-like content is not diagnostic evidence"
            )}
            if withheld else
            {"status": "clipped" if clipped else "present", "value": detail}
        ),
    }
    record["identity"] = {"runtime_id": identity_evidence(runtime_id),
                          "adapter_version": identity_evidence(adapter_version),
                          "reported_agent": identity_evidence(reported_agent)}
    if type(protocol_version) is int:
        record["identity"]["protocol_version"] = protocol_version
    if linked_to:
        record["linked_to"] = _safe_scalar(linked_to)
    remote_code = getattr(exc, "remote_code", None)
    if type(remote_code) is int:
        record["remote_code"] = remote_code
    exit_code = getattr(exc, "exit_code", None)
    if type(exit_code) is int:
        record["exit_code"] = exit_code
    return record


def append_causal_record(session_dir: str | Path, record: dict[str, Any]) -> bool:
    """Best-effort append of one already allowlisted causal envelope."""
    root = Path(session_dir)
    root_fd = None
    try:
        if (record.get("schema") != SCHEMA or type(record.get("version")) is not int
                or record["version"] != SCHEMA_VERSION):
            return False
        payload = json.dumps(record, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(payload) > MAX_CAUSAL_RECORD_BYTES:
            return False
        root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0))
        flags = os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(CAUSE_FILE, flags, 0o600, dir_fd=root_fd)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_mode & 0o077):
                return False
            size = info.st_size
            existing = os.pread(fd, MAX_CAUSE_FILE_BYTES, 0) if size else b""
            count = existing.count(b"\n")
            has_original = b'"kind":"execution_failure"' in existing
            is_original = record.get("kind") == "execution_failure"
            if count >= MAX_CAUSES or (not has_original and not is_original
                                       and count >= MAX_CAUSES - 1):
                return False
            reserved = 0 if has_original or is_original else MAX_CAUSAL_RECORD_BYTES
            if size + len(payload) + 1 > MAX_CAUSE_FILE_BYTES - reserved:
                return False
            output = payload + b"\n"
            written = 0
            while written < len(output):
                count = os.write(fd, output[written:])
                if count <= 0:
                    raise OSError("causal evidence write made no progress")
                written += count
            return True
        finally:
            os.close(fd)
    except (OSError, TypeError, ValueError):
        return False
    finally:
        if root_fd is not None:
            os.close(root_fd)


def _jsonl(store, relative: Path, *, limit: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    try:
        data = _read(store, relative, limit=limit)
    except RecordError as exc:
        return [], _field("unavailable", reason=str(exc), source=str(relative))
    records: list[dict[str, Any]] = []
    malformed = 0
    omitted = 0
    for index, line in enumerate(data.splitlines()):
        if index >= MAX_RECORDS:
            omitted += 1
            continue
        if not line.strip():
            continue
        if len(line) > MAX_LINE_BYTES:
            malformed += 1
            continue
        try:
            records.append(parse_json(line))
        except RecordError:
            malformed += 1
    status = "clipped" if omitted else "present"
    return records, _field(
        status,
        {"records": len(records), "malformed": malformed, "omitted": omitted},
        reason="record scan limit reached" if omitted else None,
        source=str(relative),
    )


def _record_value(record: dict[str, Any], source: str) -> dict[str, Any] | None:
    code = _safe_scalar(record.get("code"))
    phase = _safe_scalar(record.get("phase"), limit=64)
    boundary = _safe_scalar(record.get("boundary"), limit=128)
    if not code or not phase or not boundary:
        return None
    detail = record.get("detail") if isinstance(record.get("detail"), dict) else {}
    identity = record.get("identity", {})
    if not isinstance(identity, dict):
        return None
    def recorded_identity(value):
        if not isinstance(value, dict):
            return identity_evidence(value, source)  # Sparse earlier version-1 captures.
        status = value.get("status")
        if status == "present":
            return identity_evidence(value.get("value"), source)
        if isinstance(status, str) and status in {"clipped", "withheld", "unavailable"}:
            return _field(status, reason="recorded identity was unavailable, withheld or clipped", source=source)
        return _field("unavailable", reason="unsupported identity evidence shape", source=source)
    detail_status = detail.get("status")
    if detail_status == "withheld":
        detail_field = _field(
            "withheld",
            reason=_safe_scalar(detail.get("reason")) or "explanatory text was withheld",
            source=source,
        )
    else:
        detail_field = _detail_field(
            detail.get("value", ""), source, already_clipped=detail_status == "clipped"
        )
    return {
        "code": code,
        "phase": phase,
        "boundary": boundary,
        "detail": detail_field,
        "identity": {
            **{key: recorded_identity(identity.get(key))
               for key in ("runtime_id", "adapter_version", "reported_agent")},
            "protocol_version": _field("present", identity["protocol_version"], source=source)
                if type(identity.get("protocol_version")) is int else
                _field("unavailable", reason="protocol version was not recorded", source=source),
        },
        **({"remote_code": record["remote_code"]} if type(record.get("remote_code")) is int else {}),
        **({"exit_code": record["exit_code"]} if type(record.get("exit_code")) is int else {}),
        **({"linked_to": value} if (value := _safe_scalar(record.get("linked_to"))) else {}),
    }


def _native_cause(events: list[dict[str, Any]], source: str = "events.jsonl:session_end") -> dict[str, Any] | None:
    for record in events:
        if record.get("type") != "session_end":
            continue
        payload = record.get("payload") if isinstance(record.get("payload"), dict) else {}
        if payload.get("success") is not False:
            continue
        reason = _safe_scalar(payload.get("reason"))
        detail_source = payload.get("error") or payload.get("reason")
        return {
            **({"code": reason} if reason else {}),
            "success": False,
            "phase": "execution",
            "boundary": "native-runtime",
            "detail": _detail_field(detail_source, source) if detail_source is not None else
                _field("unavailable", reason="failure explanation was not retained", source=source),
            "identity": {
                "runtime_id": _field("present", "native", source="events.jsonl:session_end"),
                **{key: _field("unavailable", reason="identity was not retained in the native failure",
                               source="events.jsonl:session_end")
                   for key in ("adapter_version", "reported_agent", "protocol_version")},
            },
        }
    return None



def _reference_fields(mapping: dict, keys: tuple[str, ...], *, limit: int = 256,
                      statuses: dict | None = None, prefix: str = "") -> dict:
    values, coverage = {}, {}
    for key in keys:
        stored_status = (statuses or {}).get(prefix + key)
        if isinstance(stored_status, str) and stored_status in {"clipped", "withheld"}:
            field = _field(stored_status, reason="reference text was limited at persistence")
        elif key in mapping:
            field = identity_evidence(mapping[key], limit=limit)
        else:
            continue
        coverage[key] = field
        if field["status"] == "present":
            values[key] = field["value"]
    if coverage:
        values["coverage"] = coverage
    return values


def _limited_references(value: Any) -> bool:
    if isinstance(value, dict):
        if value.get("status") in {"clipped", "withheld"}:
            return True
        return any(_limited_references(item) for item in value.values())
    return isinstance(value, list) and any(_limited_references(item) for item in value)

def _bounded_identity(meta: dict[str, Any], acp_events: list[dict[str, Any]]) -> dict[str, Any]:
    segments = meta.get("runtime_segments") if isinstance(meta.get("runtime_segments"), list) else []
    safe_segments = []
    for segment in segments[:32]:
        if not isinstance(segment, dict):
            continue
        safe_segments.append(_reference_fields(
            segment, ("runtime_id", "kind", "version", "native_session_id"),
        ))
    reported_models: list[str] = []
    reported_agents: list[dict[str, str]] = []
    executions: list[dict[str, Any]] = []
    model_flags: set[str] = set()
    agent_flags: set[str] = set()
    for event in acp_events:
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
        statuses = payload.get("evidence_status")
        statuses = statuses if isinstance(statuses, dict) else {}
        stored_model_status = statuses.get("usage.model")
        if isinstance(stored_model_status, str) and stored_model_status in {"clipped", "withheld"}:
            model_flags.add(stored_model_status)
        model = identity_evidence(usage.get("model"))
        if model["status"] in {"clipped", "withheld"}:
            model_flags.add(model["status"])
        elif model["status"] == "present" and not stored_model_status \
                and model["value"] not in reported_models:
            reported_models.append(model["value"])
        segment = payload.get("agent_segment") \
            if isinstance(payload.get("agent_segment"), dict) else {}
        reported = {}
        for key in ("reported_agent", "reported_version"):
            stored_status = statuses.get("agent_segment." + key)
            value = identity_evidence(segment.get(key))
            if isinstance(stored_status, str) and stored_status in {"clipped", "withheld"}:
                agent_flags.add(stored_status)
            elif value["status"] in {"clipped", "withheld"}:
                agent_flags.add(value["status"])
            elif value["status"] == "present":
                reported[key] = value["value"]
        if reported and reported not in reported_agents:
            reported_agents.append(reported)
        execution = _reference_fields(
            segment, ("id", "digest", "runtime", "kind", "native_session_id"),
            statuses=statuses, prefix="agent_segment.",
        )
        if execution and execution not in executions:
            executions.append(execution)
    def evidence(value: Any, source: str, *, omitted: int = 0) -> dict[str, Any]:
        available = value is not None and value != []
        return _field(
            "clipped" if available and (omitted or _limited_references(value)) else "present" if available else "unavailable",
            value if available else None,
            reason=f"{omitted} identity records omitted" if omitted else
                None if available else "identity was not recorded",
            source=source,
        )

    def reported_evidence(value: list, source: str, flags: set[str]) -> dict[str, Any]:
        if flags:
            return _field("withheld" if not value and flags == {"withheld"} else "clipped",
                          value[:16] if value or flags != {"withheld"} else None,
                          reason="reported identity text was withheld or exceeded its byte limit",
                          source=source)
        return evidence(value[:16], source, omitted=max(0, len(value) - 16))

    role = meta.get("role")
    role_fields = role if isinstance(role, dict) else {}
    return {
        "agent": identity_evidence(meta.get("agent"), "meta.json:agent"),
        "agent_digest": identity_evidence(meta.get("agent_digest"), "meta.json:agent_digest"),
        "requested_model": identity_evidence(meta.get("model"), "meta.json:model"),
        "role": identity_evidence(role_fields.get("name", role_fields.get("role"))
                                if role_fields else role, "meta.json:role"),
        "role_digest": identity_evidence(role_fields.get("digest"), "meta.json:role.digest"),
        "role_model": identity_evidence(role_fields.get("model_id"), "meta.json:role.model_id"),
        "runtime_segments": evidence(safe_segments, "meta.json:runtime_segments",
                                     omitted=max(0, len(segments) - len(safe_segments))),
        "runtime_segments_omitted": max(0, len(segments) - len(safe_segments)),
        "executions": evidence(executions[:16], "acp-events.jsonl:outbound_prompt",
                               omitted=max(0, len(executions) - 16)),
        "reported_models": reported_evidence(reported_models, "acp-events.jsonl:usage_update", model_flags),
        "reported_agents": reported_evidence(reported_agents, "acp-events.jsonl:outbound_prompt", agent_flags),
    }


def _workspace_references(meta: dict[str, Any]) -> dict[str, Any]:
    refs = _reference_fields(meta, (
        "workspace", "branch", "baseline_workspace", "baseline_inherited_from",
    ), limit=1024)
    baseline = meta.get("baseline") if isinstance(meta.get("baseline"), dict) else {}
    baseline_ref = _reference_fields(baseline, ("state", "commit", "prefix"), limit=1024)
    if baseline_ref:
        refs["baseline"] = baseline_ref
    for key in ("flow_evidence_baseline", "flow_evidence_manifest"):
        value = meta.get(key)
        if isinstance(value, dict):
            refs[key] = _reference_fields(value, ("digest", "path"), limit=1024)
            for field in ("version", "size"):
                if type(value.get(field)) is int:
                    refs[key][field] = value[field]
        elif value is not None:
            selected = _reference_fields(meta, (key,), limit=1024)
            refs.update({name: item for name, item in selected.items() if name != "coverage"})
            refs.setdefault("coverage", {}).update(selected.get("coverage", {}))
    return refs


def _evidence_meta(meta: dict[str, Any]) -> dict[str, Any]:
    selected = dict(meta)
    state = meta.get("state") if isinstance(meta.get("state"), dict) else {}
    for key in ("self_check", "verification"):
        if selected.get(key) is None and isinstance(state.get(key), dict):
            selected[key] = state[key]
    receipts = meta.get("acceptance_receipts")
    if selected.get("acceptance") is None and isinstance(receipts, list):
        statuses: dict[str, int] = {}
        for receipt in receipts:
            if isinstance(receipt, dict) and isinstance(receipt.get("status"), str):
                status = _safe_scalar(receipt["status"], limit=64) or "unknown"
                statuses[status] = statuses.get(status, 0) + 1
        selected["acceptance"] = {
            "status": "recorded",
            "total": len(receipts),
            "passed": statuses.get("passed", 0),
            "failed": statuses.get("failed", 0),
        }
    return selected


def _selected_meta(meta: dict[str, Any], key: str, source: str) -> dict[str, Any]:
    value = meta.get(key)
    if value is None:
        return _field("unavailable", reason=f"no recorded {key} evidence", source=source)
    if not isinstance(value, dict):
        return _field("withheld", reason=f"recorded {key} evidence has an unsupported shape", source=source)
    allowed = {
        "review": ("policy", "status", "verdict", "valid", "rounds", "reviewer"),
        "self_check": ("status", "passed", "valid", "summary"),
        "acceptance": ("status", "passed", "failed", "total", "authority"),
        "verification": ("status", "authority", "passed", "failed", "total"),
    }[key]
    safe: dict[str, Any] = {}
    clipped = False
    for field_name in allowed:
        item = value.get(field_name)
        if isinstance(item, str):
            safe[field_name], item_clipped = _safe_text(item)
            clipped = clipped or item_clipped
        elif isinstance(item, (bool, int, float)) and not isinstance(item, complex):
            safe[field_name] = item
    for collection in ("checks", "receipts", "findings"):
        if isinstance(value.get(collection), list):
            safe[f"{collection}_count"] = len(value[collection])
    if not safe:
        return _field("withheld", reason=f"recorded {key} evidence has no allowlisted fields", source=source)
    return _field("clipped" if clipped else "present", safe,
                  reason=f"recorded {key} text exceeded the field limit" if clipped else None,
                  source=source)


def _approval_evidence(store, session_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    relative = Path(session_id) / "approvals"
    try:
        names = list_json(store, relative)
    except RecordError as exc:
        unavailable = _field("unavailable", reason=str(exc), source="approvals/")
        return unavailable, unavailable
    counts = {"requests": 0, "decisions": 0, "deliveries": 0, "malformed": 0}
    outcomes: dict[str, int] = {}
    for name in names[:MAX_RECORDS]:
        kind = next((part for part in ("request", "decision", "delivery")
                     if name.endswith(f".{part}.json")), None)
        if kind is None:
            continue
        try:
            record = _read_json(store, relative / name)
        except RecordError:
            counts["malformed"] += 1
            continue
        counts[f"{kind}s"] += 1
        if kind == "decision" and isinstance(record.get("outcome"), str):
            outcome = _safe_scalar(record["outcome"], limit=64) or "unknown"
            outcomes[outcome] = outcomes.get(outcome, 0) + 1
    value = {**counts, "outcomes": outcomes,
             "omitted": max(0, len(names) - MAX_RECORDS)}
    status = "clipped" if len(names) > MAX_RECORDS else "present"
    field = _field(status, value, reason="approval record limit reached" if status == "clipped" else None,
                   source="approvals/")
    return field, _field(status, {"records": sum(counts.values()), "omitted": value["omitted"]},
                         reason="approval record limit reached" if status == "clipped" else None,
                         source="approvals/")


def _linked_child_cause(
    store, session_id: str, project_id: str | None
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Read at most one failed flow child's retained cause after identity checks."""
    if not isinstance(project_id, str) or not project_id:
        return None, _field("unavailable", reason="parent project identity was not recorded",
                            source="meta.json:project_id")
    relative = Path(session_id) / "flow" / "receipts"
    try:
        names = list_json(store, relative)
    except RecordError as exc:
        return None, _field("unavailable", reason=str(exc), source="flow/receipts/")
    remaining = MAX_RECEIPT_BYTES
    candidates: list[str] = []
    malformed = 0
    for name in names[:MAX_CAUSES]:
        if remaining <= 0:
            break
        limit = min(MAX_LINE_BYTES, remaining)
        try:
            raw = _read(store, relative / name, limit=limit)
            remaining -= len(raw)
            receipt = parse_json(raw)
        except RecordError:
            malformed += 1
            continue
        if receipt.get("status") != "stopped":
            continue
        if isinstance(receipt.get("session_id"), str):
            candidates.append(receipt["session_id"])
        members = receipt.get("members") or []
        if not isinstance(members, list):
            malformed += 1
            continue
        for member in members:
            if isinstance(member, dict) and member.get("success") is False \
                    and isinstance(member.get("session_id"), str):
                candidates.append(member["session_id"])
    for child_id in candidates:
        try:
            child_id = validate_session_ref(child_id)
            child_meta = store.load_meta(child_id)
        except (RecordError, FileNotFoundError, ValueError):
            continue
        flow_step = child_meta.get("flow_step")
        if (
            child_meta.get("session_id") != child_id
            or child_meta.get("project_id") != project_id
            or not isinstance(flow_step, dict)
            or flow_step.get("flow_session") != session_id
        ):
            continue
        causes, _coverage = _jsonl(
            store, Path(child_id) / CAUSE_FILE, limit=MAX_CAUSE_FILE_BYTES
        )
        for index, record in enumerate(causes[:MAX_CAUSES]):
            if record.get("schema") == SCHEMA and type(record.get("version")) is int \
                    and record["version"] == SCHEMA_VERSION \
                    and record.get("kind") == "execution_failure":
                value = _record_value(record, f"{child_id}/{CAUSE_FILE}:{index + 1}")
                if value is not None:
                    value["child_session_id"] = _safe_scalar(child_id)
                    return value, _field(
                        "present",
                        {"validated": 1, "malformed_receipts": malformed},
                        source="flow/receipts/",
                    )
        native, _coverage = _jsonl(
            store, Path(child_id) / "events.jsonl", limit=MAX_SOURCE_BYTES
        )
        if value := _native_cause(native, source=f"{child_id}/events.jsonl:session_end"):
            value["child_session_id"] = _safe_scalar(child_id)
            return value, _field("present", {"validated": 1, "malformed_receipts": malformed},
                                 source="flow/receipts/")
    clipped = len(names) > MAX_CAUSES or remaining <= 0 or (
        isinstance(store, BoundedSessionRecords) and store.remaining <= 65536
    )
    return None, _field(
        "clipped" if clipped else "present",
        {"validated": 0, "malformed_receipts": malformed,
         "omitted": max(0, len(names) - MAX_CAUSES)},
        reason="source or receipt read limit reached" if clipped else None,
        source="flow/receipts/",
    )


class SessionFailureEvidence:
    """One shared reader over a selected session store."""

    def __init__(self, store):
        self.root = Path(store.root)

    def project(
        self, session_id: str, *, expected_project_id: str | None = None
    ) -> dict[str, Any]:
        session_id = validate_session_ref(session_id)
        store = BoundedSessionRecords(self.root, byte_budget=MAX_AGGREGATE_SOURCE_BYTES)
        try:
            meta = store.load_meta(session_id)
        except (RecordError, FileNotFoundError, ValueError) as exc:
            raise FailureEvidenceError(f"session evidence is unavailable: {exc}") from exc
        if meta.get("session_id") != session_id:
            raise FailureEvidenceError("session identity conflicts with its selected directory")
        if expected_project_id is not None and meta.get("project_id") != expected_project_id:
            raise FailureEvidenceError("session belongs to a different project")

        cause_records, causal_coverage = _jsonl(
            store, Path(session_id) / CAUSE_FILE, limit=MAX_CAUSE_FILE_BYTES
        )
        native, native_coverage = _jsonl(
            store, Path(session_id) / "events.jsonl", limit=MAX_SOURCE_BYTES
        )
        acp, acp_coverage = _jsonl(
            store, Path(session_id) / "acp-events.jsonl", limit=MAX_SOURCE_BYTES
        )

        valid_causes = []
        for index, record in enumerate(cause_records[:MAX_CAUSES]):
            if (record.get("schema") != SCHEMA or type(record.get("version")) is not int
                    or record["version"] != SCHEMA_VERSION
                    or not isinstance(record.get("kind"), str) or record["kind"] not in _CAUSE_KINDS):
                continue
            value = _record_value(record, f"{CAUSE_FILE}:{index + 1}")
            if value is not None:
                valid_causes.append((record.get("kind"), value))
        invalid_causes = len(cause_records) - len(valid_causes)
        if invalid_causes:
            causal_value = dict(causal_coverage.get("value") or {})
            causal_value["unsupported_or_invalid"] = invalid_causes
            causal_coverage = _field(
                "clipped",
                causal_value,
                reason="unsupported or invalid causal records were not projected",
                source=CAUSE_FILE,
            )
        omitted_causes = max(0, len(cause_records) - MAX_CAUSES)
        original = next((value for kind, value in valid_causes if kind == "execution_failure"), None)
        if original is None:
            original = _native_cause(native)
        linked_coverage = _field(
            "unavailable", reason="session is not a flow", source="flow/receipts/"
        )
        if original is None and meta.get("kind") == "flow":
            original, linked_coverage = _linked_child_cause(
                store, session_id, _safe_scalar(meta.get("project_id"))
            )
        observation = _field("unavailable", reason="no failed native outcome was retained")
        if original is not None and original.get("success") is False:
            observation = _field("present", {
                "success": False,
                **({"child_session_id": original["child_session_id"]}
                   if "child_session_id" in original else {}),
            }, source=original["detail"]["source"])
            if "code" not in original and original["detail"]["status"] == "unavailable":
                original = None
        if original is not None:
            original["cause_id"] = "original"
        original_field = _field(
            "present", original, source=original["detail"]["source"]
        ) if original else _field(
            "unavailable",
            reason="no retained original execution cause; sparse history was not reconstructed",
            source=CAUSE_FILE,
        )

        subsequent = [
            {"kind": kind, "linked_to": value.get("linked_to") or
             ("original" if original is not None else None), **value}
            for kind, value in valid_causes
            if kind in {"recovery_refusal", "cleanup_uncertainty"}
        ]
        flow_stop = meta.get("flow_stop")
        if isinstance(flow_stop, dict) and _safe_scalar(flow_stop.get("code")):
            subsequent.append({
                "kind": "recovery_refusal" if str(flow_stop["code"]).startswith("flow.quota") else "flow_stop",
                "code": _safe_scalar(flow_stop["code"]),
                "phase": "recovery" if str(flow_stop["code"]).startswith("flow.quota") else "flow",
                "boundary": "flow-engine",
                "linked_to": "original" if original is not None else None,
                "detail": _detail_field(
                    flow_stop.get("message", flow_stop["code"]), "meta.json:flow_stop"
                ),
            })
        recovery = meta.get("quota_recovery")
        if isinstance(recovery, dict) and isinstance(recovery.get("state"), str) \
                and recovery["state"] in {"blocked", "unsupported", "cancelled"}:
            subsequent.append({
                "kind": "recovery_refusal", "code": "flow.recovery." + recovery["state"],
                "phase": "recovery", "boundary": "flow-recovery",
                "linked_to": "original" if original is not None else None,
                "detail": _detail_field(recovery.get("reason", ""), "meta.json:quota_recovery"),
            })

        workspace = _workspace_references(meta)
        selected_meta = _evidence_meta(meta)
        approvals, approvals_coverage = _approval_evidence(store, session_id)
        projection = {
            "schema": SCHEMA,
            "version": SCHEMA_VERSION,
            "session": _field("present", {
                "session_id": _safe_scalar(session_id),
                "project_id": _safe_scalar(meta.get("project_id")),
                "recorded_outcome": _safe_scalar(meta.get("status")),
            }, source="meta.json"),
            "original_cause": original_field,
            "failure_observation": observation,
            "subsequent_failures": _field(
                "clipped" if omitted_causes else "present",
                subsequent,
                reason=f"{omitted_causes} causal records omitted" if omitted_causes else None,
                source=CAUSE_FILE,
            ),
            "identity": _field("present", _bounded_identity(meta, acp), source="meta.json,acp-events.jsonl"),
            "workspace_references": _field(
                "clipped" if _limited_references(workspace) else "present" if workspace else "unavailable",
                workspace or None,
                reason=None if workspace else "no stored workspace or checkpoint references",
                source="meta.json",
            ),
            "approvals": approvals,
            "review": _selected_meta(selected_meta, "review", "meta.json:review"),
            "self_check": _selected_meta(selected_meta, "self_check", "meta.json:state.self_check"),
            "acceptance": _selected_meta(selected_meta, "acceptance", "meta.json:acceptance"),
            "verification": _selected_meta(selected_meta, "verification", "meta.json:state.verification"),
            "coverage": {
                "native_events": native_coverage,
                "acp_events": acp_coverage,
                "causal_records": causal_coverage,
                "approvals": approvals_coverage,
                "review": _field("present" if meta.get("review") is not None else "unavailable",
                                  source="meta.json"),
                "acceptance": _field("present" if selected_meta.get("acceptance") is not None else "unavailable",
                                      source="meta.json"),
                "selected_diagnostics": self._snapshot_coverage(store, session_id),
                "linked_children": linked_coverage,
            },
            "limits": {
                "projection_bytes": MAX_PROJECTION_BYTES,
                "text_bytes": MAX_TEXT_BYTES,
                "causal_records": MAX_CAUSES,
                "source_file_bytes": MAX_SOURCE_BYTES,
                "aggregate_source_bytes": MAX_AGGREGATE_SOURCE_BYTES,
            },
        }
        encoded = json.dumps(projection, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(encoded) > MAX_PROJECTION_BYTES:
            projection["subsequent_failures"] = _field(
                "clipped", [], reason="projection byte limit reached", source=CAUSE_FILE
            )
            encoded = json.dumps(projection, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(encoded) > MAX_PROJECTION_BYTES:
            raise FailureEvidenceError("bounded failure projection exceeds its serialized limit")
        projection["serialized_bytes"] = 0
        while projection["serialized_bytes"] != len(encoded := json.dumps(
            projection, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")):
            projection["serialized_bytes"] = len(encoded)
        if len(encoded) > MAX_PROJECTION_BYTES:
            raise FailureEvidenceError("bounded failure projection exceeds its serialized limit")
        try:
            latest_meta = _read_json(store, Path(session_id) / "meta.json")
        except RecordError as exc:
            raise FailureEvidenceError(f"session changed while evidence was read: {exc}") from exc
        if latest_meta != meta:
            raise FailureEvidenceError("session changed while evidence was read")
        return projection

    @staticmethod
    def _snapshot_coverage(store, session_id: str) -> dict[str, Any]:
        try:
            names = list_json(store, Path(session_id) / DIAGNOSTIC_DIR, entry_limit=MAX_SNAPSHOT_ENTRIES)
            reference_digest = session_reference_digest(store, session_id)
        except RecordError:
            return _field("unavailable", reason="no selected diagnostic snapshots", source=DIAGNOSTIC_DIR)
        active = expired = malformed = 0
        now = time.time()
        for name in names[:MAX_SNAPSHOTS]:
            try:
                snapshot = _read_json(store, Path(session_id) / DIAGNOSTIC_DIR / name)
            except RecordError:
                malformed += 1
                continue
            preview = snapshot.get("preview")
            if (snapshot.get("schema") != "garuda.diagnostic-snapshot"
                    or type(snapshot.get("version")) is not int or snapshot["version"] != 1
                    or not isinstance(preview, dict)
                    or preview.get("schema") != "garuda.diagnostic-preview"
                    or type(preview.get("version")) is not int or preview["version"] != 1
                    or preview.get("session_id") != _safe_scalar(session_id)
                    or preview.get("session_ref_digest") != reference_digest
                    or not isinstance(preview.get("json"), str)):
                malformed += 1
                continue
            encoded = preview["json"].encode("utf-8")
            if (len(encoded) > MAX_PROJECTION_BYTES or type(preview.get("bytes")) is not int
                    or preview["bytes"] != len(encoded)):
                malformed += 1
                continue
            try:
                if not isinstance(preview.get("categories"), list):
                    raise FailureEvidenceError("snapshot categories are unavailable")
                categories = DiagnosticService._categories(preview["categories"])
                if preview.get("digest") != _preview_digest(reference_digest, categories, preview["json"]):
                    raise FailureEvidenceError("snapshot confirmation binding is invalid")
                selected = parse_json(encoded)
            except (RecordError, FailureEvidenceError):
                malformed += 1
                continue
            if selected.get("schema") != SCHEMA or type(selected.get("version")) is not int \
                    or selected["version"] != SCHEMA_VERSION:
                malformed += 1
                continue
            expires_at = snapshot.get("expires_at")
            created_at = snapshot.get("created_at")
            if (type(created_at) not in (int, float) or type(expires_at) not in (int, float)
                    or created_at > now + 60 or not 0 < expires_at - created_at <= SNAPSHOT_RETENTION_SECONDS + 1):
                malformed += 1
                continue
            if isinstance(expires_at, (int, float)) and not isinstance(expires_at, bool) \
                    and expires_at > now:
                active += 1
            else:
                expired += 1
        if not active:
            return _field(
                "unavailable",
                reason="no unexpired selected diagnostic snapshots",
                source=DIAGNOSTIC_DIR,
            )
        return _field("present", {"snapshots": active, "expired": expired,
                                  "malformed": malformed,
                                  "omitted": max(0, len(names) - MAX_SNAPSHOTS)},
                      source=DIAGNOSTIC_DIR)


@dataclass
class DiagnosticService:
    store: Any

    def _reader(self) -> SessionFailureEvidence:
        return SessionFailureEvidence(self.store)

    @staticmethod
    def _categories(categories: list[str] | tuple[str, ...] | None) -> tuple[str, ...]:
        if categories is not None and (
            not isinstance(categories, (list, tuple)) or not categories
            or len(categories) > len(_CATEGORIES)
            or any(not isinstance(item, str) for item in categories)
        ):
            raise FailureEvidenceError("diagnostic categories must be a bounded nonempty list")
        selected = tuple(dict.fromkeys(categories or sorted(_CATEGORIES)))
        if not selected or any(item not in _CATEGORIES for item in selected):
            raise FailureEvidenceError(
                "diagnostic categories must be selected from " + ", ".join(sorted(_CATEGORIES))
            )
        return selected

    def preview(
        self,
        session_id: str,
        *,
        categories: list[str] | tuple[str, ...] | None = None,
        expected_project_id: str | None = None,
    ) -> dict[str, Any]:
        selected = self._categories(categories)
        session_id = validate_session_ref(session_id)
        try:
            reference_digest = session_reference_digest(self.store, session_id)
        except RecordError as exc:
            raise FailureEvidenceError("selected session reference is unavailable") from exc
        full = self._reader().project(session_id, expected_project_id=expected_project_id)
        try:
            if reference_digest != session_reference_digest(self.store, session_id):
                raise FailureEvidenceError("selected session changed during preview")
        except RecordError as exc:
            raise FailureEvidenceError("selected session changed during preview") from exc
        keys = {"causes": ("original_cause", "failure_observation", "subsequent_failures"),
                "identities": ("session", "identity"),
                "workspace": ("workspace_references",),
                "approvals": ("approvals",),
                "review": ("review", "self_check"),
                "acceptance": ("acceptance", "verification")}
        document: dict[str, Any] = {"schema": SCHEMA, "version": SCHEMA_VERSION}
        for category in selected:
            for key in keys[category]:
                document[key] = full[key]
        document["coverage"] = full["coverage"]
        document["limits"] = full["limits"]
        raw = json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        size = len(raw.encode("utf-8"))
        if size > MAX_PROJECTION_BYTES:
            raise FailureEvidenceError("selected diagnostic preview exceeds its byte limit")
        digest = _preview_digest(reference_digest, selected, raw)
        return {
            "schema": "garuda.diagnostic-preview",
            "version": 1,
            "session_id": _safe_scalar(validate_session_ref(session_id)),
            "session_ref_digest": reference_digest,
            "categories": list(selected),
            "json": raw,
            "bytes": size,
            "digest": digest,
            "retention_seconds": SNAPSHOT_RETENTION_SECONDS,
            "snapshot_limit": MAX_SNAPSHOTS,
            "snapshot_directory_entries": MAX_SNAPSHOT_ENTRIES,
            "snapshot_bytes": MAX_SNAPSHOT_BYTES,
        }

    def capture(
        self,
        session_id: str,
        *,
        categories: list[str] | tuple[str, ...],
        preview_digest: str,
        expected_project_id: str | None = None,
    ) -> dict[str, Any]:
        if not categories:
            raise FailureEvidenceError("capture requires explicit diagnostic categories")
        if not re.fullmatch(r"[0-9a-f]{64}", preview_digest or ""):
            raise FailureEvidenceError("capture requires the exact preview digest")
        session_id = validate_session_ref(session_id)
        directory_flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
        root_fd = session_fd = diagnostic_fd = None
        try:
            root_fd = os.open(Path(self.store.root).resolve(), directory_flags)
            session_fd = os.open(session_id, directory_flags, dir_fd=root_fd)
            session_identity = os.fstat(session_fd)
            if session_identity.st_uid != os.getuid():
                raise FailureEvidenceError("diagnostic session belongs to another owner")
            # Serialize preview revalidation, retention and immutable publication
            # across captures without adding a lock file to read-only previews.
            fcntl.flock(session_fd, fcntl.LOCK_EX)
            reference_digest = session_reference_digest(
                self.store, session_id, root_fd=root_fd, session_fd=session_fd
            )
            preview = self.preview(session_id, categories=categories,
                                   expected_project_id=expected_project_id)
            if preview["digest"] != preview_digest or preview["session_ref_digest"] != reference_digest:
                raise FailureEvidenceError("diagnostic evidence changed; create a fresh preview")
            current = os.stat(session_id, dir_fd=root_fd, follow_symlinks=False)
            if (current.st_dev, current.st_ino) != (session_identity.st_dev, session_identity.st_ino):
                raise FailureEvidenceError("diagnostic session changed during capture")
            try:
                os.mkdir(DIAGNOSTIC_DIR, mode=0o700, dir_fd=session_fd)
            except FileExistsError:
                pass
            try:
                diagnostic_fd = os.open(DIAGNOSTIC_DIR, directory_flags, dir_fd=session_fd)
            except OSError as exc:
                raise FailureEvidenceError("diagnostic snapshot directory is not a regular directory") from exc
            info = os.fstat(diagnostic_fd)
            if info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise FailureEvidenceError("diagnostic snapshot directory is not owner-only")
            now = time.time()
            names = []
            total = 0
            # Inventory is bounded before any mutation. Under the session lock no
            # supported capture writer can still own a staging file.
            inventory = []
            with os.scandir(diagnostic_fd) as entries:
                for index, entry in enumerate(entries):
                    if index >= MAX_SNAPSHOT_ENTRIES:
                        raise FailureEvidenceError("diagnostic directory entry limit is full")
                    inventory.append((entry.name, entry.stat(follow_symlinks=False)))
            for name, entry_info in inventory:
                owned_regular = (stat.S_ISREG(entry_info.st_mode)
                                 and entry_info.st_uid == os.getuid()
                                 and not entry_info.st_mode & 0o077)
                staging = re.fullmatch(r"\.[0-9]+-[0-9a-f]{32}\.tmp", name)
                if not owned_regular or not (staging or name.endswith(".json")):
                    raise FailureEvidenceError("diagnostic directory contains unsupported entries")
                if staging or now - entry_info.st_mtime > SNAPSHOT_RETENTION_SECONDS:
                    os.unlink(name, dir_fd=diagnostic_fd)
                    continue
                names.append(name)
                total += entry_info.st_size
                if len(names) >= MAX_SNAPSHOTS or total >= MAX_SNAPSHOT_BYTES:
                    raise FailureEvidenceError("diagnostic snapshot retention limit is full")
            payload = {
                "schema": "garuda.diagnostic-snapshot", "version": 1,
                "created_at": now, "expires_at": now + SNAPSHOT_RETENTION_SECONDS,
                "preview": preview,
            }
            raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
            if total + len(raw) > MAX_SNAPSHOT_BYTES:
                raise FailureEvidenceError("diagnostic snapshot retention limit is full")
            snapshot_id = f"{int(now)}-{uuid.uuid4().hex}"
            target = f"{snapshot_id}.json"
            temporary = f".{snapshot_id}.tmp"
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
            try:
                fd = os.open(temporary, flags, 0o600, dir_fd=diagnostic_fd)
                try:
                    written = 0
                    while written < len(raw):
                        count = os.write(fd, raw[written:])
                        if count <= 0:
                            raise OSError("diagnostic snapshot write made no progress")
                        written += count
                    os.fsync(fd)
                finally:
                    os.close(fd)
                os.link(temporary, target, src_dir_fd=diagnostic_fd,
                        dst_dir_fd=diagnostic_fd, follow_symlinks=False)
                os.fsync(diagnostic_fd)
                current = os.stat(session_id, dir_fd=root_fd, follow_symlinks=False)
                latest = os.stat(DIAGNOSTIC_DIR, dir_fd=session_fd, follow_symlinks=False)
                if (current.st_dev, current.st_ino, latest.st_dev, latest.st_ino) != (
                    session_identity.st_dev, session_identity.st_ino, info.st_dev, info.st_ino
                ):
                    raise FailureEvidenceError("diagnostic session changed during publication")
            finally:
                try:
                    os.unlink(temporary, dir_fd=diagnostic_fd)
                except FileNotFoundError:
                    pass
            return {"snapshot_id": snapshot_id, "bytes": len(raw), "digest": preview_digest,
                    "expires_at": payload["expires_at"]}
        except OSError as exc:
            raise FailureEvidenceError(f"diagnostic snapshot publication did not complete: {exc}") from exc
        finally:
            for fd in (diagnostic_fd, session_fd, root_fd):
                if fd is not None:
                    os.close(fd)

    def export(
        self,
        session_id: str,
        *,
        categories: list[str] | tuple[str, ...] | None = None,
        expected_project_id: str | None = None,
    ) -> str:
        return self.preview(session_id, categories=categories,
                            expected_project_id=expected_project_id)["json"]
