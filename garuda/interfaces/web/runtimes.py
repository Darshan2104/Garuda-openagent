"""Dashboard runtime controls backing store (P1.7, issue #38).

Pure functions over a session store and parsed manifests — the route layer
stays thin, and every shape here is what the UI renders directly. Reads never
mutate; the two mutating calls (prepare, recover-run) are gated on write mode
by the routes. Nothing is invented: unavailable runtimes, unknown versions,
and unknown quotas render as such.
"""

from __future__ import annotations

from typing import Any

from garuda.acp.catalog import (
    discover,
    health_of,
)
from garuda.runtime.recovery import recover, report_to_dict
from garuda.workspace.diff import session_delta


def manifests_from_extra(extra: dict) -> list[dict]:
    """Configured manifests: explicit extras win, builtins plus native otherwise."""
    raw = extra.get("manifests")
    if raw is None:
        from garuda.interfaces.runtime_cli import load_configured_manifest_dicts

        return load_configured_manifest_dicts({})
    if not isinstance(raw, list):
        raise ValueError("context manifests must be a list of manifest dicts")
    return raw


def list_runtimes(manifest_dicts: list[dict]) -> list[dict[str, Any]]:
    from garuda.runtime.registry import parse_global_manifests

    manifests = parse_global_manifests(manifest_dicts, source="dashboard runtimes")
    return [health_of(entry) for entry in discover(manifests)]


def inspect_runtime(manifest_dicts: list[dict], runtime_id: str) -> dict[str, Any] | None:
    from garuda.runtime.registry import parse_global_manifests

    manifests = parse_global_manifests(manifest_dicts, source="dashboard runtimes")
    found = {entry.runtime_id: entry for entry in discover(manifests)}
    entry = found.get(runtime_id)
    if entry is None:
        return None
    record = health_of(entry)
    record["auth_guidance"] = entry.describe_auth()
    return record


def handoff_preview(store, session_id: str, target_id: str) -> dict[str, Any]:
    """Read-only switch preview. Never mutates the session."""
    unified = store.load_unified(session_id)
    return {
        "session_id": session_id,
        "target_runtime": target_id,
        "source_runtime": unified.active.runtime_id,
        "native_session_id": unified.active.native_session_id,
        "handoff_state": unified.handoff.get("state", "none"),
        "task": unified.legacy.get("task", ""),
        "requires_confirm": True,
    }


def handoff_prepare(store, session_id: str, target_id: str, *, pack_manager=None) -> dict[str, Any]:
    """Compile the handoff package and record it prepared. Write-mode only."""
    from garuda.context.pack import compile_handoff
    from garuda.context.state_card import WorkingState

    unified = store.load_unified(session_id)
    state = WorkingState(task=unified.legacy.get("task", session_id))
    doc, body = compile_handoff(
        state,
        source_runtime=unified.active.runtime_id,
        session_id=session_id,
        native_session_id=unified.active.native_session_id or "",
    )
    if pack_manager is None:
        from garuda.context.pack import ContextPackManager

        pack_manager = ContextPackManager(store.session_dir(session_id))
    pack_manager.write_handoff(doc, body)
    store.record_handoff(session_id, state="prepared", attempts=1, target_runtime=target_id)
    return {
        "session_id": session_id,
        "target_runtime": target_id,
        "handoff_state": "prepared",
        "handoff_file": str(pack_manager.handoff_path),
    }


def diff_timeline(store, session_id: str) -> dict[str, Any]:
    """Baseline plus authoritative delta for the session workspace.

    Reads the baseline the session recorded at start — never a fresh
    capture, which could not distinguish pre-existing dirt from agent work.
    Sessions without a recorded baseline report that explicitly with an
    empty file list rather than an invented one.
    """
    meta = store.load_meta(session_id)
    workspace = meta.get("workspace", ".")
    baseline_data = meta.get("baseline") or {}
    from garuda.workspace.diff import Baseline

    if not baseline_data:
        return {
            "session_id": session_id,
            "baseline_commit": "",
            "baseline_recorded": False,
            "files": [],
            "note": "no baseline recorded for this session",
        }
    baseline = Baseline.from_dict(baseline_data)
    delta = session_delta(baseline, workspace)
    return {
        "session_id": session_id,
        "baseline_commit": delta.baseline_commit,
        "baseline_recorded": True,
        "files": [
            {"path": f.path, "kind": f.kind, "preexisting": f.preexisting}
            for f in delta.files
        ],
    }


def recover_report(store, session_id: str, *, run: bool = False) -> dict[str, Any]:
    """Classify (read-only) or recover (write-mode) a session."""
    from garuda.runtime.recovery import RecoveryError, classify

    if run:
        try:
            return report_to_dict(recover(store, session_id))
        except RecoveryError as exc:
            return {"session_id": session_id, "error": str(exc)}
    try:
        return report_to_dict(classify(store, session_id))
    except RecoveryError as exc:
        return {"session_id": session_id, "error": str(exc)}
