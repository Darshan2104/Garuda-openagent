"""Context-only forks of saved native checkpoints; never recover the source."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from garuda.core.session_records import (
    MAX_RECORD_BYTES,
    RecordError,
    UnsafeRecordError,
    parse_json,
    parse_json_value,
    read_bytes,
)
from garuda.core.sessions import message_from_dict
from garuda.types import Message, Role

CONTEXT_REWIND_NOTICE = (
    "Context rewind only: filesystem and processes were not restored. "
    "All inherited candidate and verification evidence is stale; recheck the current workspace. "
    "Source side effects and receipts are historical references, not authority to replay or clean up."
)


def validate_fork_request(*, fork, fork_at, resume=None, context=None, isolation="shared"):
    if fork is None:
        if fork_at is not None:
            raise ValueError("--fork-at requires --fork")
        return
    if not isinstance(fork, str) or not fork:
        raise ValueError("--fork requires a saved session reference")
    if type(fork_at) is not int or fork_at < 0:
        raise ValueError("--fork requires a non-negative integer --fork-at message index")
    if resume is not None or context is not None or isolation != "shared":
        raise ValueError("context fork cannot combine with resume, a supplied context, or workspace isolation")


def preview_context_fork(store, reference: str, fork_at: int, *, workspace: str, all_projects: bool = False) -> dict:
    """Validate the same saved prefix as execution without admitting any work."""
    from garuda.core.artifact_read_model import MAX_ARTIFACT_SOURCE_BYTES
    from garuda.core.session_records import BoundedSessionRecords

    validate_fork_request(fork=reference, fork_at=fork_at)
    reader = BoundedSessionRecords(store.root, byte_budget=MAX_ARTIFACT_SOURCE_BYTES)
    session_id = reader.resolve_for_inspection(reference, workspace=workspace, all_projects=all_projects)
    with reader.selected_session(session_id) as binding:
        plan = prepare_context_fork(reader, session_id, fork_at, bounded_reads=True)
        plan.check_source(reader, binding)
        return {
            **plan.record,
            "retained_messages": len(plan.messages),
            "admission": "not checked; execution requires ordinary workspace and capacity admission",
            "artifact_inheritance": "not checked; execution builds resumed context and inherits artifacts after admission",
        }


def _snapshot(reader, session_id, *, bounded_reads=False):
    snapshot = {}
    for name in ("meta.json", "messages.json", "state.json", "events.jsonl"):
        try:
            relative = Path(session_id) / name
            limit = None if name in {"messages.json", "events.jsonl"} else MAX_RECORD_BYTES
            if bounded_reads:
                snapshot[name] = reader.read(relative, limit=reader.remaining if limit is None else limit)
            else:
                snapshot[name] = read_bytes(reader, relative, limit=limit)
        except RecordError as exc:
            if (name in {"state.json", "events.jsonl"}
                    and not isinstance(exc, UnsafeRecordError)
                    and isinstance(exc.__cause__, FileNotFoundError)):
                snapshot[name] = None
            else:
                raise
    return snapshot


def _digests(snapshot):
    return {name: hashlib.sha256(raw).hexdigest() if raw is not None else None
            for name, raw in snapshot.items()}


def _complete_prefix(messages, fork_at):
    if fork_at >= len(messages):
        raise ValueError("fork point is outside the saved message checkpoint")
    pending = set()
    complete = 0
    for index, message in enumerate(messages[:fork_at + 1]):
        if message.role == Role.TOOL:
            if message.tool_call_id not in pending or message.tool_calls:
                raise ValueError("fork prefix contains an unmatched tool result")
            pending.remove(message.tool_call_id)
        elif pending:
            raise ValueError("fork prefix contains an interrupted tool-call batch")
        elif message.tool_calls:
            ids = [call.id for call in message.tool_calls]
            if message.role != Role.ASSISTANT or not all(ids) or len(ids) != len(set(ids)):
                raise ValueError("fork prefix contains an ambiguous tool-call batch")
            pending = set(ids)
        if not pending:
            complete = index + 1
    return messages[:complete], complete - 1


@dataclass
class ContextFork:
    messages: list[Message]
    initial_state: dict
    record: dict
    bounded_reads: bool = False

    def check_source(self, reader, source_binding):
        source_binding.check()
        snapshot = _snapshot(reader, self.record["forked_from"], bounded_reads=self.bounded_reads)
        if _digests(snapshot) != self.record["source_files"]:
            raise ValueError("context fork source changed during preparation")


def _messages(data):
    values = parse_json_value(data)
    if not isinstance(values, list):
        raise RecordError("saved messages must be a JSON list")
    messages = []
    for value in values:
        if (not isinstance(value, dict) or not isinstance(value.get("role"), str)
                or not isinstance(value.get("content", ""), str)
                or not isinstance(value.get("metadata", {}), dict)):
            raise RecordError("saved message has an invalid shape")
        for field in ("name", "tool_call_id"):
            if value.get(field) is not None and not isinstance(value[field], str):
                raise RecordError(f"saved message {field} must be a string")
        calls = value.get("tool_calls")
        if calls is not None:
            if not isinstance(calls, list) or any(
                not isinstance(call, dict) or not isinstance(call.get("id"), str)
                or not isinstance(call.get("name"), str) for call in calls
            ):
                raise RecordError("saved tool calls have an invalid shape")
        try:
            messages.append(message_from_dict(value))
        except (KeyError, TypeError, ValueError) as exc:
            raise RecordError("saved message has an invalid role or tool call") from exc
    return messages


def prepare_context_fork(reader, session_id: str, fork_at: int, *, bounded_reads: bool = False) -> ContextFork:
    snapshot = _snapshot(reader, session_id, bounded_reads=bounded_reads)
    meta = parse_json(snapshot["meta.json"])
    if meta != reader.load_meta(session_id):
        raise ValueError("fork source metadata changed after selection")
    if meta.get("session_id") != session_id:
        raise ValueError("fork source identity conflicts with its selected directory")
    from garuda.runtime.session_state import from_legacy, validate

    if meta.get("status") not in ("success", "failed", "cancelled"):
        raise RecordError("context fork requires a terminal native source")
    handoff = meta.get("handoff")
    if handoff is not None and not isinstance(handoff, dict):
        raise RecordError("saved handoff must be a mapping")
    segments = meta.get("runtime_segments")
    if segments is not None and (not isinstance(segments, list)
                                 or any(not isinstance(segment, dict) for segment in segments)):
        raise RecordError("saved runtime segments must be a list of mappings")
    if "state" in meta:
        state = meta["state"]
        if not isinstance(state, dict) or any(
            state.get(field) is not None and not isinstance(state[field], dict)
            for field in ("verification", "self_check")
        ):
            raise RecordError("saved session state has an invalid shape")
    source_state = validate(meta["state"]) if "state" in meta else from_legacy(meta)
    if (source_state["process"] != "exited" or source_state["work"] not in {"done", "stopped"}
            or meta.get("kind") == "flow"
            or meta.get("quarantine") or (handoff or {}).get("state", "none") != "none"
            or (segments and (segments[-1].get("kind") != "native"
                              or segments[-1].get("runtime_id") != "native"))):
        raise ValueError("context fork requires a terminal native source without pending ownership or quarantine")
    messages, effective_at = _complete_prefix(_messages(snapshot["messages.json"]), fork_at)
    state = parse_json(snapshot["state.json"]) if snapshot["state.json"] is not None else {}
    source_files = _digests(snapshot)
    digest = hashlib.sha256(json.dumps(source_files, sort_keys=True).encode()).hexdigest()
    references = [
        {"session_id": session_id, "path": "meta.json", "purpose": "task constraints and receipt references"},
        {"session_id": session_id, "path": "state.json", "purpose": "historical working state",
         "availability": "recorded" if snapshot["state.json"] is not None else "unknown"},
        {"session_id": session_id, "path": "events.jsonl", "purpose": "side effects and verification receipts",
         "availability": "recorded" if snapshot["events.jsonl"] is not None else "unknown"},
        {"session_id": session_id, "path": "receipts/", "purpose": "receipt references", "availability": "unknown"},
    ]
    receipt_references = []
    receipt_coverage = "unknown"
    try:
        names, omitted = reader.names(Path(session_id) / "receipts", limit=64)
    except RecordError as exc:
        if isinstance(exc, UnsafeRecordError) or not isinstance(exc.__cause__, FileNotFoundError):
            raise
    else:
        receipt_coverage = "partial" if omitted else "complete"
        receipt_references = [{"session_id": session_id, "path": f"receipts/{name}",
                               "availability": "unknown", "evidence": "stale"}
                              for name in names if name.endswith(".json")]
    record = {
        "forked_from": session_id,
        "fork_at": effective_at,
        "fork_requested_at": fork_at,
        "fork_adjustment": "incomplete tool-call batch" if effective_at != fork_at else None,
        "source_digest": digest,
        "source_files": source_files,
        "rewind": "context rewind only",
        "filesystem_restored": False,
        "processes_restored": False,
        "inherited_evidence": {"candidate": "stale", "verification": "stale"},
        "references": references,
        "receipt_references": receipt_references,
        "receipt_reference_coverage": receipt_coverage,
    }
    # Carry constraints, never the source's candidate/check/todo claims as live state.
    initial_state = {"goal": state.get("goal", ""), "acceptance": state.get("acceptance", ""),
                     "context_fork": record, "source_task": meta.get("task", "")}
    return ContextFork(messages, initial_state, record, bounded_reads=bounded_reads)
