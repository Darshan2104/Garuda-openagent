"""Artifact inheritance after the native resume owner has authorized a settled source."""
from __future__ import annotations

from garuda.context.manager import ContextManager
from garuda.core.artifact_read_model import (
    MAX_VERIFY_BYTES,
    artifact_inheritance_record,
    retained_capture_descriptors,
    validate_descriptor,
)
from garuda.core.buffer import ArtifactSourceDirectory, ToolOutputBuffer
from garuda.core.child_result import (
    canonical_json,
    rebind_child_result_storage,
    validate_child_result_envelope,
)
from garuda.core.session_records import BoundedSessionRecords
from garuda.core.sessions import SessionStore


def inherit_session_artifacts(
    store: SessionStore, source_session: str, target_session: str, context: ContextManager,
    *, threshold_bytes: int, source_records: BoundedSessionRecords,
    source_binding: ArtifactSourceDirectory,
) -> ToolOutputBuffer:
    """Copy through the byte owner and rebind only facts from this selected source."""
    if context._buffer is not None:
        raise ValueError("resume requires a context without a pre-bound buffer")
    source_binding.check()
    if source_records.root.resolve() != store.root.resolve():
        raise ValueError("resume source reader belongs to a different store")
    previous = ToolOutputBuffer(source_session, root=store.session_dir(source_session) / "buffers")
    current = ToolOutputBuffer(target_session, threshold_bytes=threshold_bytes,
                               root=store.session_dir(target_session) / "buffers")
    retained, unknown, _ = retained_capture_descriptors(
        source_records, source_session, verification_bytes=MAX_VERIFY_BYTES,
    )
    inheritance = artifact_inheritance_record(
        source_session, target_session, retained, "unknown" if unknown else "complete",
    )
    current.inherit_from(previous, source_binding=source_binding)
    for capture in retained:
        rebound = validate_descriptor(capture)
        if rebound is not None and rebound["owner"]["kind"] == "buffer":
            rebound["storage_session_id"] = target_session
            current.retain_inherited_commitment(rebound)
    if unknown:
        current.require_inherited_commitments()
    for message in context.get_messages():
        metadata = message.metadata
        if "artifact_capture" in metadata:
            capture = validate_descriptor(metadata["artifact_capture"])
            if capture is None:
                metadata["artifact_capture"] = None
            elif capture["storage_session_id"] == source_session:
                capture["storage_session_id"] = target_session
                metadata["artifact_capture"] = validate_descriptor(capture)
        child = validate_child_result_envelope(metadata.get("child_result"))
        if child is not None and child["delivery"]["state"] == "referenced":
            original = canonical_json(child)
            if not message.content.endswith(original):
                raise ValueError("resumed child handoff does not match its canonical metadata")
            rebound = rebind_child_result_storage(child, source_session, target_session)
            message.content = message.content[:-len(original)] + canonical_json(rebound)
            metadata["child_result"] = rebound
    store.checkpoint_artifact_inheritance(target_session, inheritance)
    return current
