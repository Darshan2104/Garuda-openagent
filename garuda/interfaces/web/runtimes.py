"""Dashboard runtime controls backing store (P1.7, issue #38).

Pure functions over a session store and parsed manifests — the route layer
stays thin, and every shape here is what the UI renders directly. Reads never
mutate; the two mutating calls (prepare, recover-run) are gated on write mode
by the routes. Nothing is invented: unavailable runtimes, unknown versions,
and unknown quotas render as such.
"""

from __future__ import annotations

from typing import Any

from garuda.acp.catalog import discover, health_of, load_trusted_disabled
from garuda.runtime import RegistryError
from garuda.runtime.recovery import recover, report_to_dict
from garuda.runtime.registry import RuntimeRegistry, parse_global_manifests
from garuda.workspace.diff import session_delta


def manifests_from_extra(extra: dict, workspace: str = ".") -> list[dict]:
    """Compatibility helper for callers that explicitly provide manifests.

    Keep legacy callers on the trusted global settings path; dashboard routes
    use :func:`registry_from_context` directly so aliases and disablement are
    enforced as well.
    """
    raw = extra.get("manifests")
    if raw is None:
        from garuda.acp.catalog import load_trusted_runtime_settings
        from garuda.interfaces.runtime_cli import load_configured_manifest_dicts

        return load_configured_manifest_dicts(load_trusted_runtime_settings())
    if not isinstance(raw, list):
        raise ValueError("context manifests must be a list of manifest dicts")
    return raw


def registry_from_context(extra: dict | None = None, workspace: str = ".") -> RuntimeRegistry:
    """Resolve dashboard runtimes through the trusted product registry.

    An explicit ``manifests`` list remains a deterministic test seam. The
    production dashboard has no such override and therefore reads the same
    global settings/project aliases as the CLI and SDK.
    """
    extra = extra or {}
    raw = extra.get("manifests")
    if raw is None:
        from garuda.interfaces.runtime_cli import configured_registry

        return configured_registry(workspace)
    if not isinstance(raw, list):
        raise ValueError("context manifests must be a list of manifest dicts")
    return RuntimeRegistry(
        parse_global_manifests(raw, source="dashboard runtimes"),
        disabled=load_trusted_disabled(),
    )


def _discovered(registry: RuntimeRegistry):
    return discover(registry.manifests, disabled=registry.disabled_ids)


def _require_target(registry: RuntimeRegistry, target_id: str):
    """Resolve and availability-check a handoff target before any mutation."""
    target = registry.get(target_id)
    if target.kind.value == "native":
        return target
    found = {entry.runtime_id: entry for entry in _discovered(registry)}
    entry = found.get(target.runtime_id)
    if entry is None or not entry.available:
        detail = "; ".join(entry.warnings) if entry else "not discovered"
        raise RegistryError(f"runtime {target_id!r} is unavailable: {detail}")
    return target


def list_runtimes(
    manifest_dicts: list[dict] | None = None, *, workspace: str = ".", extra: dict | None = None
) -> list[dict[str, Any]]:
    context = extra or (
        {"manifests": manifest_dicts} if manifest_dicts is not None else {}
    )
    registry = registry_from_context(context, workspace)
    return [health_of(entry) for entry in _discovered(registry)]


def inspect_runtime(
    manifest_dicts: list[dict] | None, runtime_id: str, *, workspace: str = ".", extra: dict | None = None
) -> dict[str, Any] | None:
    context = extra or (
        {"manifests": manifest_dicts} if manifest_dicts is not None else {}
    )
    registry = registry_from_context(context, workspace)
    found = {entry.runtime_id: entry for entry in _discovered(registry)}
    entry = found.get(runtime_id)
    if entry is None:
        return None
    record = health_of(entry)
    record["auth_guidance"] = entry.describe_auth()
    return record


def handoff_preview(
    store, session_id: str, target_id: str, *, registry: RuntimeRegistry | None = None
) -> dict[str, Any]:
    """Read-only switch preview. Never mutates the session."""
    if registry is not None:
        _require_target(registry, target_id)
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


def handoff_prepare(
    store,
    session_id: str,
    target_id: str,
    *,
    pack_manager=None,
    registry: RuntimeRegistry | None = None,
) -> dict[str, Any]:
    """Compile the handoff package and record it prepared. Write-mode only."""
    from garuda.context.pack import compile_handoff
    from garuda.context.state_card import WorkingState

    if registry is not None:
        _require_target(registry, target_id)
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
