"""CLI runtime selection and handoff commands (P1.6, issue #37).

`garuda runtime list|inspect` renders discovery; `garuda run --runtime`
selects the executor (native default, unchanged); `garuda runtime handoff`
previews by default and prepares only with `--confirm`; `garuda runtime
recover` classifies and recovers sessions. Text by default, JSON with
`--json`, and no switch ever happens without explicit confirmation.
"""

from __future__ import annotations

import json
from typing import Any

from garuda.acp.catalog import (
    adapter_for_manifest,
    builtin_manifest_dicts,
    discover,
    health_of,
    require_acp_argv,
)
from garuda.runtime.recovery import recover, report_to_dict


def load_configured_manifest_dicts(global_settings: dict | None = None) -> list[dict]:
    """Builtin manifests plus the trusted global `runtimes:` list.

    The in-process native loop is always first, mirroring the registry's
    builtin entry, so listings and selection never lose the default.
    """
    native = {
        "runtime_id": "native",
        "kind": "native",
        "version": "builtin",
        "description": "The in-process Garuda loop.",
    }
    dicts = builtin_manifest_dicts()
    extra = (global_settings or {}).get("runtimes", [])
    if not isinstance(extra, list):
        raise ValueError("global settings 'runtimes' must be a list of manifests")
    return [native, *dicts, *extra]


def cmd_list(manifests, *, as_json: bool = False) -> str:
    found = discover(manifests)
    if as_json:
        return json.dumps([health_of(d) for d in found], indent=2)
    lines = []
    for entry in found:
        lines.extend(entry.describe())
    return "\n".join(lines) + "\n"


def cmd_inspect(manifests, runtime_id: str, *, as_json: bool = False) -> str:
    found = {d.runtime_id: d for d in discover(manifests)}
    if runtime_id not in found:
        raise KeyError(f"unknown runtime {runtime_id!r}")
    entry = found[runtime_id]
    if as_json:
        return json.dumps(health_of(entry), indent=2)
    return "\n".join([*entry.describe(), *entry.describe_auth()]) + "\n"


def cmd_handoff_preview(store, session_id: str, target_id: str) -> str:
    """Show what a switch would do. Reads only; changes nothing."""
    unified = store.load_unified(session_id)
    lines = [
        f"handoff preview: {session_id} -> {target_id}",
        f"  source runtime: {unified.active.runtime_id} "
        f"(native session {unified.active.native_session_id})",
        f"  changed files: {len(unified.legacy.get('changed_files', [])) or 'see diff'}",
        f"  handoff state: {unified.handoff.get('state', 'none')}",
        "  no switch happens without --confirm",
    ]
    return "\n".join(lines) + "\n"


async def cmd_handoff_confirm(
    store, session_id: str, target_id: str, *, pack_manager=None, state=None
) -> str:
    """Prepare the handoff package and record it. Requires --confirm upstream."""
    from garuda.context.pack import compile_handoff

    unified = store.load_unified(session_id)
    if state is None:
        from garuda.context.state_card import WorkingState

        state = WorkingState(task=unified.legacy.get("task", session_id))
    doc, body = compile_handoff(
        state,
        source_runtime=unified.active.runtime_id,
        session_id=session_id,
        native_session_id=unified.active.native_session_id or "",
    )
    if pack_manager is not None:
        pack_manager.write_handoff(doc, body)
    store.record_handoff(session_id, state="prepared", attempts=1, target_runtime=target_id)
    return f"handoff prepared: {session_id} -> {target_id}\n"


def cmd_recover(store, session_id: str, *, as_json: bool = False) -> str:
    report = recover(store, session_id)
    if as_json:
        return json.dumps(report_to_dict(report), indent=2)
    lines = [
        f"session {report.session_id}: {report.state.value}",
        f"  resume: {report.resume_session_id}",
    ]
    lines.extend(f"  note: {note}" for note in report.notes)
    return "\n".join(lines) + "\n"


async def run_acp_task(manifest, task: str, *, session_id: str | None = None) -> dict[str, Any]:
    """Run one task on an ACP runtime, streaming normalized events to stdout."""
    found = {d.runtime_id: d for d in discover([manifest])}
    entry = found.get(manifest.runtime_id)
    argv = require_acp_argv(
        manifest, executable=entry.executable if entry else None
    )
    runtime = adapter_for_manifest(manifest, argv_override=list(argv))
    info = await runtime.start(task=task, session_id=session_id)
    try:
        turn = await runtime.prompt(task)
        events, _ = await runtime.poll_events(0)
        for event in events:
            print(f"[{event.kind.value} t{event.turn}] {event.payload}")
        return {"session_id": info.native_session_id, "turn": turn, "events": len(events)}
    finally:
        await runtime.close()


__all__ = [
    "cmd_handoff_confirm",
    "cmd_handoff_preview",
    "cmd_inspect",
    "cmd_list",
    "cmd_recover",
    "load_configured_manifest_dicts",
    "run_acp_task",
]
