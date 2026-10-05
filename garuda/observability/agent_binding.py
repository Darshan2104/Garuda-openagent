"""Source-free links from sending executions to recorded runtime tenures."""
from garuda.runtime.session import UnifiedSessionError


def active_runtime_tenure(store, session_id, *, runtime_id, kind, native_session_id):
    """Snapshot only an active tenure that matches the sending runtime."""
    if store is None:
        return None
    try:
        unified = store.load_unified(session_id)
    except (OSError, ValueError, UnifiedSessionError):
        return None
    active = unified.active
    if (active.runtime_id != runtime_id or active.kind != kind
            or active.native_session_id != native_session_id):
        return None
    return {"index": len(unified.segments) - 1, "runtime_id": runtime_id,
            "kind": kind, "native_session_id": native_session_id}


def recorded_runtime_tenure(binding, meta):
    """Validate a recorded index and full neutral reference; never infer one."""
    value = binding.get("runtime_tenure")
    if not isinstance(value, dict):
        return None
    index = value.get("index")
    tenures = meta.get("runtime_segments")
    if (type(index) is not int or index < 0 or not isinstance(tenures, list)
            or index >= len(tenures) or not isinstance(tenures[index], dict)):
        return None
    tenure = tenures[index]
    expected_kind = "native" if binding["kind"] == "native_execution" else "acp"
    if (value.get("runtime_id") != binding["runtime"] or value.get("kind") != expected_kind
            or any(value.get(key) != tenure.get(key)
                   for key in ("runtime_id", "kind", "native_session_id"))):
        return None
    return {"index": index, "runtime_id": value["runtime_id"], "kind": expected_kind,
            "native_session_id": value.get("native_session_id")}
