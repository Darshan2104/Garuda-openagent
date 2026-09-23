"""CLI runtime selection, handoff, resume, and recovery commands (P1.6, issue #37).

`garuda runtime list|inspect` renders discovery; `garuda run --runtime`
selects the executor (native default, unchanged); `garuda runtime handoff`
previews by default and executes the handoff transaction only with
`--confirm` (pause → checkpoint → capture → start target → acknowledge or
rollback); `garuda runtime resume` continues a persisted session through the
real run lifecycle (which classifies first); `garuda runtime recover`
classifies and recovers sessions. Text by default, JSON with `--json`, and
no switch ever happens without explicit confirmation.
"""

from __future__ import annotations

import json
from typing import Any

from garuda.acp.catalog import (
    adapter_for_manifest,
    adapter_for_registry,
    builtin_manifest_dicts,
    discover,
    health_of,
    load_trusted_disabled,
    require_acp_argv,
)
from garuda.runtime.recovery import recover, report_to_dict
from garuda.runtime.registry import RuntimeRegistry, parse_global_manifests


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
    store,
    session_id: str,
    target_id: str,
    *,
    manifests=None,
    workspace: str | None = None,
    pack_manager=None,
    state=None,
    target_argv_override: list[str] | None = None,
    disabled=None,
) -> str:
    """Execute the handoff transaction against real runtimes.

    Resolves the target through the shared registry (unknown or disabled
    targets are refused before anything moves), resumes the source session
    as a live native runtime, then runs pause → checkpoint → capture →
    start-target → acknowledge-or-rollback. Success transfers the single
    mutating ownership; target failure rolls back to the resumable source.
    A native target is not a switch: resume the session instead.
    """
    from garuda.context.pack import compile_handoff
    from garuda.context.state_card import WorkingState
    from garuda.runtime.handoff import execute_handoff
    from garuda.runtime.native import NativeGarudaRuntime

    if target_id == "native":
        return (
            "handoff refused: the target is the native runtime itself — "
            "resume the session instead (`garuda runtime resume`).\n"
        )
    parsed = parse_global_manifests(
        [m for m in (manifests or []) if m.get("runtime_id") != "native"],
        source="runtime handoff",
    )
    if disabled is None:
        disabled = load_trusted_disabled()
    registry = RuntimeRegistry(parsed, disabled=disabled)
    resolved = registry.get(target_id)  # unknown/disabled fail closed here
    if target_argv_override is None:
        import shutil

        from garuda.acp.catalog import require_acp_argv

        executable = shutil.which(resolved.command[0]) if resolved.command else None
        require_acp_argv(resolved, executable=executable)
    unified = store.load_unified(session_id)
    if state is None:
        try:
            state = WorkingState.from_dict(store.load_state(session_id))
        except Exception:
            state = WorkingState(task=unified.legacy.get("task", session_id))
        if not state.task:
            state.task = unified.legacy.get("task", session_id)
    if workspace is None:
        workspace = store.load_meta(session_id).get("workspace", ".")

    def _checkpoint() -> None:
        if pack_manager is not None:
            doc, body = compile_handoff(
                state,
                source_runtime=unified.active.runtime_id,
                session_id=session_id,
                native_session_id=unified.active.native_session_id or "",
            )
            pack_manager.write_handoff(doc, body)

    source = NativeGarudaRuntime(
        agent=None, model=None, tools=[], config=None, permissions=None, store=store
    )
    await source.resume(native_session_id=session_id)

    def _target_factory():
        return adapter_for_registry(
            registry, target_id, argv_override=target_argv_override
        )

    tx, _ = await execute_handoff(
        session_id=session_id,
        source=source,
        target_factory=_target_factory,
        store=store,
        checkpoint=_checkpoint,
        workspace=workspace,
    )
    return (
        f"handoff acknowledged: {session_id} -> {target_id} "
        f"(phase={tx.phase.value})\n"
    )


async def cmd_resume(
    *,
    store,
    session_id: str,
    task: str,
    model,
    agent,
    tools,
    config,
    permissions,
    workspace: str,
    **runner_kwargs,
) -> str:
    """Resume a persisted session through the real run lifecycle.

    Classification runs first (prepared switches roll back, ambiguous trails
    refuse); the restored working state and conversation then continue as a
    new session. Returns a human-readable summary naming both sessions.
    """
    from garuda.core.events import EventStore
    from garuda.interfaces.runner import run_agent_task

    events = EventStore()
    result = await run_agent_task(
        task=task,
        model=model,
        agent=agent,
        tools=tools,
        config=config,
        permissions=permissions,
        workspace=workspace,
        events=events,
        store=store,
        resume=session_id,
        **runner_kwargs,
    )
    return (
        f"resumed {session_id} as {events.session_id}: "
        f"{'success' if result.success else 'failed'} "
        f"in {result.turns} turns\n"
    )


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
    "cmd_resume",
    "load_configured_manifest_dicts",
    "run_acp_task",
]
