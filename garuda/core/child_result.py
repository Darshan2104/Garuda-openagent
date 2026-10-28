"""Bounded delivery of a native subagent's schema-accepted JSON output."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from copy import deepcopy
from typing import Any, Protocol

from garuda.agents.output_schema import DIALECT
from garuda.types import AgentResult

CONTRACT_VERSION = 1
INLINE_OUTPUT_BYTES = 8 * 1024
MAX_ENVELOPE_BYTES = 64 * 1024
MAX_IDENTITY_CHARS = 512
_HANDOFF_LABEL = "Validated child result:\n"
_STATUS_LABEL = "Child result status:\n"
_SUMMARY_OMITTED = "[… summary omitted for typed handoff]"
_SHA256 = re.compile(r"sha256:[0-9a-f]{64}\Z")
_UNAVAILABLE_REASONS = frozenset({
    "no_accepted_validated_output",
    "invalid_validation_provenance",
    "producer_identity_unavailable",
    "parent_storage_unavailable",
    "parent_retrieval_tool_unavailable",
    "publication_failed",
    "delivery_budget_too_small",
})


class ChildResultStorage(Protocol):
    session_id: str

    def store(
        self, buffer_id: str, content: str, tool_name: str = "", is_error: bool = False
    ) -> Any: ...

    def read(self, buffer_id: str) -> str: ...


class ChildResultRetrievalError(ValueError):
    """A child-result reference is unavailable or no longer matches its receipt."""


def canonical_json(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def _digest(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def schema_identity(schema: dict | bool) -> dict[str, str]:
    return {"dialect": DIALECT, "digest": _digest(canonical_json(schema))}


def accepted_output_contract(schema: dict | bool, output: Any) -> dict[str, Any]:
    return {
        "version": CONTRACT_VERSION,
        "declared": True,
        "validated": True,
        "present": True,
        "schema": schema_identity(schema),
        "schema_document": deepcopy(schema),
        "output": output,
    }


def declared_output_contract(schema: dict | bool) -> dict[str, Any]:
    return {
        "version": CONTRACT_VERSION,
        "declared": True,
        "validated": False,
        "present": False,
        "schema": schema_identity(schema),
        "schema_document": deepcopy(schema),
    }


def _bounded_identity(value: Any) -> str | None:
    return value if isinstance(value, str) and 0 < len(value) <= MAX_IDENTITY_CHARS else None


def _valid_output_contract(contract: Any, *, accepted: bool) -> bool:
    if not isinstance(contract, dict):
        return False
    allowed = {
        "version", "declared", "validated", "present", "schema", "schema_document",
        "output", "agent_name", "agent_digest",
    }
    if set(contract) - allowed:
        return False
    if type(contract.get("version")) is not int or contract["version"] != CONTRACT_VERSION:
        return False
    if contract.get("declared") is not True:
        return False
    schema = contract.get("schema_document")
    identity = contract.get("schema")
    if not isinstance(schema, (dict, bool)) or identity != schema_identity(schema):
        return False
    for key in ("agent_name", "agent_digest"):
        if key in contract and contract[key] is not None and _bounded_identity(contract[key]) is None:
            return False
    if accepted:
        if contract.get("validated") is not True or contract.get("present") is not True:
            return False
        if "output" not in contract:
            return False
        try:
            canonical_json(contract["output"])
        except (TypeError, ValueError):
            return False
        return True
    return (
        contract.get("validated") is False
        and contract.get("present") is False
        and "output" not in contract
    )


def _producer(result: AgentResult, profile: str) -> dict[str, Any] | None:
    contract = result.metadata.get("output_contract")
    session_id = _bounded_identity(result.metadata.get("subagent_session_id"))
    recorded_profile = _bounded_identity(result.metadata.get("subagent_profile"))
    resolved_profile = _bounded_identity(
        result.metadata.get("subagent_resolved_profile", profile)
    )
    if not isinstance(contract, dict):
        return None
    digest = _bounded_identity(contract.get("agent_digest"))
    if (
        session_id is None
        or recorded_profile != profile
        or resolved_profile is None
        or contract.get("agent_name") != resolved_profile
        or digest is None
    ):
        return None
    return {"session_id": session_id, "profile": resolved_profile, "agent_digest": digest}


def _tool_names(parent_tools: list | None) -> set[str]:
    return {str(getattr(tool, "name", "")) for tool in (parent_tools or [])}


def _normalize_workspace(workspace: Any) -> dict[str, Any] | None:
    if not isinstance(workspace, dict):
        return None
    normalized: dict[str, Any] = {}
    for key in ("kind", "attribution", "baseline_commit", "head_commit", "error"):
        value = workspace.get(key)
        if isinstance(value, str) and len(value) <= MAX_IDENTITY_CHARS:
            normalized[key] = value
    root = workspace.get("root")
    if isinstance(root, str) and len(root) <= MAX_IDENTITY_CHARS:
        normalized["root"] = root
    elif root is not None or workspace.get("root_omitted") is True:
        normalized["root_omitted"] = True
    for key in ("changed_count", "files_omitted"):
        value = workspace.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            normalized[key] = value
    files = workspace.get("files")
    if isinstance(files, list):
        kept = []
        omitted = normalized.get("files_omitted", 0)
        for item in files:
            if not isinstance(item, dict):
                omitted += 1
                continue
            path = item.get("path")
            kind = item.get("kind")
            if not isinstance(path, str) or not path or len(path) > MAX_IDENTITY_CHARS:
                omitted += 1
                continue
            if not isinstance(kind, str) or not kind or len(kind) > 64:
                omitted += 1
                continue
            record = {"path": path, "kind": kind}
            if item.get("preexisting_at_start") is True:
                record["preexisting_at_start"] = True
            renamed = item.get("renamed_from")
            if isinstance(renamed, str) and 0 < len(renamed) <= MAX_IDENTITY_CHARS:
                record["renamed_from"] = renamed
            elif renamed is not None:
                record["renamed_from_omitted"] = True
            kept.append(record)
        normalized["files"] = kept
        if omitted:
            normalized["files_omitted"] = omitted
    return normalized or None


def _fits_handoff(envelope: dict[str, Any], output_budget: int | None) -> bool:
    if output_budget is None:
        return True
    label = _HANDOFF_LABEL if envelope["outcome"]["validation"] == "accepted" else _STATUS_LABEL
    return len((label + canonical_json(envelope)).encode("utf-8")) <= output_budget


def _compact_workspace(envelope: dict[str, Any]) -> None:
    workspace = envelope.get("workspace")
    if not isinstance(workspace, dict):
        return
    files = workspace.pop("files", None)
    if isinstance(files, list) and files:
        workspace["files_omitted"] = workspace.get("files_omitted", 0) + len(files)
    if "root" in workspace:
        workspace.pop("root")
        workspace["root_omitted"] = True
    workspace.pop("error", None)


def _reference(
    *, storage: ChildResultStorage, producer_session: str, serialized: str
) -> dict[str, Any]:
    buffer_id = f"child-result-{producer_session}-{uuid.uuid4().hex}"
    ref = storage.store(buffer_id, serialized, tool_name="invoke_subagent")
    return {
        "version": CONTRACT_VERSION,
        "format": "application/json",
        "buffer_id": ref.buffer_id,
        "parent_session_id": storage.session_id,
        "producer_session_id": producer_session,
        "serialized_bytes": len(serialized.encode("utf-8")),
        "sha256": _digest(serialized),
        "retrieval": {
            "tool": "buffer_slice",
            "mode": "characters",
            "reference_argument": "child_result_reference",
        },
    }


def build_child_result_envelope(
    result: AgentResult,
    profile: str,
    *,
    storage: ChildResultStorage | None,
    parent_tools: list | None,
    output_budget: int | None,
    workspace: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build and publish one internally consistent parent-facing envelope."""
    contract = result.metadata.get("output_contract")
    declared = isinstance(contract, dict) and contract.get("declared") is True
    provenance_ok = _valid_output_contract(contract, accepted=result.success) if declared else False
    producer = _producer(result, profile)
    accepted = result.success and provenance_ok and producer is not None
    envelope: dict[str, Any] = {
        "version": CONTRACT_VERSION,
        "outcome": {
            "success": bool(result.success),
            "validation": "accepted" if accepted else "not_accepted" if declared else "not_declared",
        },
        "producer": producer or {"profile": profile},
    }
    if declared and isinstance(contract, dict) and isinstance(contract.get("schema"), dict):
        envelope["schema"] = deepcopy(contract["schema"])
    normalized_workspace = _normalize_workspace(workspace)
    if normalized_workspace is not None:
        envelope["workspace"] = normalized_workspace

    if not accepted:
        if not result.success:
            reason = "no_accepted_validated_output"
        elif not provenance_ok:
            reason = "invalid_validation_provenance"
        else:
            reason = "producer_identity_unavailable"
        envelope["delivery"] = {
            "state": "unavailable" if declared else "not_applicable",
            **({"reason": reason} if declared else {}),
        }
        if not _fits_handoff(envelope, output_budget):
            _compact_workspace(envelope)
        return envelope

    serialized = canonical_json(contract["output"])
    serialized_bytes = len(serialized.encode("utf-8"))
    inline = deepcopy(envelope)
    inline["delivery"] = {
        "state": "inline", "serialized_bytes": serialized_bytes, "sha256": _digest(serialized)
    }
    inline["output"] = deepcopy(contract["output"])
    if serialized_bytes <= INLINE_OUTPUT_BYTES and _fits_handoff(inline, output_budget):
        return inline

    if storage is None:
        reason = "parent_storage_unavailable"
    elif "buffer_slice" not in _tool_names(parent_tools):
        reason = "parent_retrieval_tool_unavailable"
    else:
        try:
            reference = _reference(
                storage=storage, producer_session=producer["session_id"], serialized=serialized
            )
        except Exception:
            reason = "publication_failed"
        else:
            envelope["delivery"] = {"state": "referenced", "reference": reference}
            if not _fits_handoff(envelope, output_budget):
                _compact_workspace(envelope)
            if _fits_handoff(envelope, output_budget):
                return envelope
            reason = "delivery_budget_too_small"
    envelope["delivery"] = {"state": "unavailable", "reason": reason}
    if not _fits_handoff(envelope, output_budget):
        _compact_workspace(envelope)
    return envelope


def _utf8_prefix(text: str, byte_limit: int) -> str:
    if byte_limit <= 0:
        return ""
    raw = text.encode("utf-8")
    return text if len(raw) <= byte_limit else raw[:byte_limit].decode("utf-8", errors="ignore")


def render_child_result_handoff(
    summary: str, envelope: dict[str, Any], output_budget: int | None
) -> str:
    if envelope.get("outcome", {}).get("validation") == "not_declared":
        return summary
    label = _HANDOFF_LABEL if envelope["outcome"]["validation"] == "accepted" else _STATUS_LABEL
    suffix = label + canonical_json(envelope)
    if output_budget is None:
        return f"{summary}\n\n{suffix}"
    suffix_bytes = len(suffix.encode("utf-8"))
    if suffix_bytes > output_budget:
        return _utf8_prefix(
            _STATUS_LABEL + canonical_json({
                "version": CONTRACT_VERSION,
                "outcome": {"success": False, "validation": "not_accepted"},
                "producer": {},
                "delivery": {"state": "unavailable", "reason": "delivery_budget_too_small"},
            }),
            output_budget,
        )
    room = output_budget - suffix_bytes
    if room <= 2:
        return suffix
    prefix = _utf8_prefix(summary, room - 2)
    if prefix != summary:
        marker_bytes = len(_SUMMARY_OMITTED.encode("utf-8"))
        if room < marker_bytes + 2:
            return suffix
        partial = _utf8_prefix(summary, max(0, room - marker_bytes - 3)).rstrip()
        prefix = f"{partial}\n{_SUMMARY_OMITTED}" if partial else _SUMMARY_OMITTED
    return f"{prefix}\n\n{suffix}" if prefix else suffix


def _strict_keys(value: dict, allowed: set[str]) -> bool:
    return set(value).issubset(allowed)


def validate_child_result_reference(
    reference: Any, storage: ChildResultStorage | None = None
) -> dict[str, Any]:
    if not isinstance(reference, dict) or not _strict_keys(reference, {
        "version", "format", "buffer_id", "parent_session_id", "producer_session_id",
        "serialized_bytes", "sha256", "retrieval",
    }):
        raise ChildResultRetrievalError("malformed child-result reference")
    if type(reference.get("version")) is not int or reference["version"] != CONTRACT_VERSION:
        raise ChildResultRetrievalError("unsupported child-result reference")
    if reference.get("format") != "application/json":
        raise ChildResultRetrievalError("unsupported child-result format")
    for key in ("buffer_id", "parent_session_id", "producer_session_id"):
        if _bounded_identity(reference.get(key)) is None:
            raise ChildResultRetrievalError(f"child-result reference has invalid {key}")
    size = reference.get("serialized_bytes")
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        raise ChildResultRetrievalError("child-result reference has invalid byte count")
    if not isinstance(reference.get("sha256"), str) or not _SHA256.fullmatch(reference["sha256"]):
        raise ChildResultRetrievalError("child-result reference has invalid digest")
    if reference.get("retrieval") != {
        "tool": "buffer_slice",
        "mode": "characters",
        "reference_argument": "child_result_reference",
    }:
        raise ChildResultRetrievalError("child-result reference has invalid retrieval contract")
    expected_prefix = f"child-result-{reference['producer_session_id']}-"
    if not reference["buffer_id"].startswith(expected_prefix) or not re.fullmatch(
        r"[0-9a-f]{32}", reference["buffer_id"][len(expected_prefix):]
    ):
        raise ChildResultRetrievalError("child-result producer does not match its buffer")
    if storage is not None and reference["parent_session_id"] != storage.session_id:
        raise ChildResultRetrievalError("child-result reference belongs to another parent session")
    return deepcopy(reference)


def read_child_result_reference(
    reference: dict[str, Any], storage: ChildResultStorage
) -> tuple[Any, str]:
    """Verify one storage snapshot and return both its value and exact JSON."""
    checked = validate_child_result_reference(reference, storage)
    try:
        serialized = storage.read(checked["buffer_id"])
    except (KeyError, OSError, UnicodeError) as exc:
        raise ChildResultRetrievalError("child-result content is unavailable") from exc
    if len(serialized.encode("utf-8")) != checked["serialized_bytes"]:
        raise ChildResultRetrievalError("child-result size does not match its reference")
    if _digest(serialized) != checked["sha256"]:
        raise ChildResultRetrievalError("child-result digest does not match its reference")
    try:
        value = json.loads(serialized)
    except json.JSONDecodeError as exc:
        raise ChildResultRetrievalError("child-result content is not valid JSON") from exc
    if canonical_json(value) != serialized:
        raise ChildResultRetrievalError("child-result content is not canonical JSON")
    return value, serialized


def validate_child_result_envelope(value: Any) -> dict[str, Any] | None:
    """Validate and normalize the only child metadata retained by ToolRunner."""
    if not isinstance(value, dict) or not _strict_keys(value, {
        "version", "outcome", "producer", "schema", "delivery", "output", "workspace",
    }):
        return None
    if type(value.get("version")) is not int or value["version"] != CONTRACT_VERSION:
        return None
    outcome = value.get("outcome")
    producer = value.get("producer")
    delivery = value.get("delivery")
    if not all(isinstance(part, dict) for part in (outcome, producer, delivery)):
        return None
    if not _strict_keys(outcome, {"success", "validation"}) or not _strict_keys(
        producer, {"session_id", "profile", "agent_digest"}
    ):
        return None
    validation = outcome.get("validation")
    if type(outcome.get("success")) is not bool or validation not in {
        "accepted", "not_accepted", "not_declared",
    }:
        return None
    for field in producer.values():
        if _bounded_identity(field) is None:
            return None
    state = delivery.get("state")
    if state not in {"inline", "referenced", "unavailable", "not_applicable"}:
        return None
    if state == "inline":
        if not _strict_keys(delivery, {"state", "serialized_bytes", "sha256"}) or "output" not in value:
            return None
        try:
            serialized = canonical_json(value["output"])
        except (TypeError, ValueError):
            return None
        size = delivery.get("serialized_bytes")
        if type(size) is not int or size < 0 or size != len(serialized.encode("utf-8")):
            return None
        if delivery.get("sha256") != _digest(serialized):
            return None
    elif state == "referenced":
        if not _strict_keys(delivery, {"state", "reference"}) or "output" in value:
            return None
        try:
            reference = validate_child_result_reference(delivery.get("reference"))
        except ChildResultRetrievalError:
            return None
        if reference["producer_session_id"] != producer.get("session_id"):
            return None
    else:
        if "output" in value or not _strict_keys(delivery, {"state", "reason"}):
            return None
        if state == "unavailable" and delivery.get("reason") not in _UNAVAILABLE_REASONS:
            return None
        if state == "not_applicable" and "reason" in delivery:
            return None
    if validation == "accepted":
        if outcome["success"] is not True or state not in {"inline", "referenced", "unavailable"}:
            return None
        if _bounded_identity(producer.get("session_id")) is None:
            return None
        if _bounded_identity(producer.get("profile")) is None:
            return None
        if _bounded_identity(producer.get("agent_digest")) is None:
            return None
    elif state in {"inline", "referenced"}:
        return None
    if validation == "not_declared":
        if state != "not_applicable" or "schema" in value:
            return None
    else:
        schema = value.get("schema")
        if not isinstance(schema, dict) or set(schema) != {"dialect", "digest"}:
            return None
        if schema.get("dialect") != DIALECT or not isinstance(schema.get("digest"), str):
            return None
        if not _SHA256.fullmatch(schema["digest"]):
            return None
    workspace = value.get("workspace")
    if workspace is not None and _normalize_workspace(workspace) != workspace:
        return None
    try:
        encoded = canonical_json(value).encode("utf-8")
    except (TypeError, ValueError):
        return None
    return deepcopy(value) if len(encoded) <= MAX_ENVELOPE_BYTES else None
