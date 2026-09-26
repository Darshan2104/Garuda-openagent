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
    BUILTIN_STUBS,
    adapter_for_registry,
    builtin_manifest_dicts,
    discover,
    health_of,
    load_trusted_disabled,
    load_trusted_runtime_settings,
    require_acp_argv,
    shared_registry,
)
from garuda.runtime import RegistryError
from garuda.runtime.recovery import recover, report_to_dict
from garuda.runtime.registry import RuntimeRegistry

RUNTIME_API_VERSION = "1"


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


def configured_registry(
    workspace: str = ".",
    *,
    disabled=None,
    global_settings: dict | None = None,
    project_settings: dict | None = None,
) -> RuntimeRegistry:
    """Build the one trusted registry used by CLI discovery and execution."""
    from garuda.config.agent_home import resolve_agent_home

    if global_settings is None or project_settings is None:
        home = resolve_agent_home(workspace)
        if global_settings is None:
            global_settings = getattr(home, "global_settings", None)
            if global_settings is None:
                global_settings = load_trusted_runtime_settings()
        if project_settings is None:
            project_settings = getattr(home, "settings", None) or {}
    extras = global_settings.get("runtimes", [])
    if not isinstance(extras, list):
        raise ValueError("global settings 'runtimes' must be a list of manifests")
    refs = project_settings.get("runtime_refs", [])
    if refs is None:
        refs = []
    if not isinstance(refs, list):
        raise ValueError("project settings 'runtime_refs' must be a list of references")
    if disabled is None:
        disabled = load_trusted_disabled(global_settings)
    return shared_registry(extra_manifests=extras, project_refs=refs, disabled=disabled)


def acp_adapter_for_workspace(
    workspace: str,
    runtime_name: str,
    *,
    argv_override: list[str] | None = None,
    disabled=None,
    policy=None,
    global_settings: dict | None = None,
    project_settings: dict | None = None,
    store=None,
    persist_dir: str | None = None,
):
    """Resolve one configured ACP id through policy and availability gates."""
    registry = configured_registry(
        workspace,
        disabled=disabled,
        global_settings=global_settings,
        project_settings=project_settings,
    )
    try:
        registry.get(runtime_name)
        manifest = registry.manifest_for(runtime_name)
    except RegistryError as exc:
        raise ValueError(f"Cannot use runtime {runtime_name!r} ({exc})") from exc
    if argv_override is None:
        found = {entry.runtime_id: entry for entry in discover([manifest])}
        entry = found.get(manifest.runtime_id)
        argv = require_acp_argv(manifest, executable=entry.executable if entry else None)
    else:
        argv = list(argv_override)
    return registry, adapter_for_registry(
        registry,
        runtime_name,
        argv_override=argv,
        policy=policy,
        store=store,
        persist_dir=persist_dir,
    )


def attach_acp_segment(store, session_id: str, adapter) -> None:
    """Persist one ACP tenure in the session's unified runtime history."""
    from garuda.runtime.session import RuntimeSegment

    unified = store.load_unified(session_id)
    if (
        unified.active.kind == "acp"
        and unified.active.runtime_id == adapter.runtime_id
        and unified.active.native_session_id == adapter.native_session_id
    ):
        return

    authority = adapter.authority
    capabilities = (
        frozenset(set(authority.owners) | set(authority.to_snapshot()))
        if authority is not None
        else frozenset()
    )
    store.attach_runtime_segment(
        session_id,
        RuntimeSegment(
            runtime_id=adapter.runtime_id,
            kind="acp",
            native_session_id=adapter.native_session_id,
            version=adapter.version,
            capabilities=capabilities,
        ),
    )


def cmd_list(manifests, *, as_json: bool = False, disabled=None) -> str:
    found = discover(manifests, disabled=disabled or frozenset())
    if as_json:
        return json.dumps([health_of(d) for d in found], indent=2)
    lines = []
    for entry in found:
        lines.extend(entry.describe())
    return "\n".join(lines) + "\n"


def cmd_list_registry(registry: RuntimeRegistry, *, as_json: bool = False) -> str:
    return cmd_list(registry.manifests, as_json=as_json, disabled=registry.disabled_ids)


def cmd_inspect(manifests, runtime_id: str, *, as_json: bool = False, disabled=None) -> str:
    found = {d.runtime_id: d for d in discover(manifests, disabled=disabled or frozenset())}
    if runtime_id not in found:
        raise KeyError(f"unknown runtime {runtime_id!r}")
    entry = found[runtime_id]
    if as_json:
        return json.dumps(health_of(entry), indent=2)
    return "\n".join([*entry.describe(), *entry.describe_auth()]) + "\n"


def cmd_inspect_registry(
    registry: RuntimeRegistry, runtime_id: str, *, as_json: bool = False
) -> str:
    return cmd_inspect(
        registry.manifests, runtime_id, as_json=as_json, disabled=registry.disabled_ids
    )


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
    if workspace is None:
        workspace = store.load_meta(session_id).get("workspace", ".")
    if manifests is None:
        registry = configured_registry(workspace, disabled=disabled)
    else:
        # Explicit manifests are an isolated test seam, never project authority.
        extras = [
            item for item in manifests
            if isinstance(item, dict) and item.get("runtime_id") not in {"native", *[s["runtime_id"] for s in BUILTIN_STUBS]}
        ]
        if disabled is None:
            disabled = load_trusted_disabled()
        registry = shared_registry(extra_manifests=extras, disabled=disabled)
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
    handoff_body: list[str] = []

    def _checkpoint() -> None:
        doc, body = compile_handoff(
            state,
            source_runtime=unified.active.runtime_id,
            session_id=session_id,
            native_session_id=unified.active.native_session_id or "",
        )
        handoff_body[:] = [body]
        if pack_manager is not None:
            pack_manager.write_handoff(doc, body)

    source = NativeGarudaRuntime(
        agent=None, model=None, tools=[], config=None, permissions=None, store=store
    )
    await source.resume(native_session_id=session_id)

    def _target_factory():
        return adapter_for_registry(
            registry,
            target_id,
            argv_override=target_argv_override,
            store=store,
            persist_dir=str(store.session_dir(session_id)),
        )

    async def _deliver(target) -> None:
        if not handoff_body:
            raise RuntimeError("handoff package was not generated")
        # The transfer is an actual target turn, not merely a file written for
        # a process that is immediately forgotten. Its subprocess remains
        # supervised until this turn finishes and the caller deterministically
        # closes it below.
        await target.prompt(handoff_body[0])
        await target.poll_events(0)

    tx, target = await execute_handoff(
        session_id=session_id,
        source=source,
        target_factory=_target_factory,
        store=store,
        checkpoint=_checkpoint,
        deliver=_deliver,
        workspace=workspace,
    )
    try:
        return (
            f"handoff acknowledged: {session_id} -> {target_id} "
            f"(phase={tx.phase.value}; handoff delivered)\n"
        )
    finally:
        await target.close()


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


def support_bundle_dict(store, session_id: str) -> dict[str, Any]:
    """Build the redacted support bundle for one persisted session."""
    from garuda import __version__ as _version
    from garuda.observability.support import build_support_bundle

    try:
        session_dir = store.session_dir(session_id)
    except Exception as exc:
        raise ValueError(f"unknown session {session_id!r}: {exc}") from exc
    if not session_dir.is_dir():
        raise ValueError(f"unknown session {session_id!r}")
    return build_support_bundle(session_dir, garuda_version=_version)


def cmd_support_bundle(store, session_id: str) -> str:
    return json.dumps(support_bundle_dict(store, session_id), indent=2)


def recover_dict(store, session_id: str) -> dict[str, Any]:
    return report_to_dict(recover(store, session_id))


async def run_acp_task(
    task: str,
    *,
    runtime_id: str,
    workspace: str = ".",
    session_id: str | None = None,
    argv_override: list[str] | None = None,
    disabled=None,
    store=None,
) -> dict[str, Any]:
    """Run one ACP task through the same trusted registry as every CLI path."""
    from garuda.core.events import EventStore
    from garuda.core.sessions import SessionStore

    store = store or SessionStore()
    events = EventStore(session_id=session_id)
    store.begin(
        events.session_id,
        task=task,
        model="acp",
        agent=runtime_id,
        workspace=workspace,
    )
    _registry, runtime = acp_adapter_for_workspace(
        workspace,
        runtime_id,
        argv_override=argv_override,
        disabled=disabled,
        store=store,
        persist_dir=str(store.session_dir(events.session_id)),
    )
    info = await runtime.start(task=task, session_id=events.session_id)
    try:
        turn = await runtime.prompt(task)
        trail, _ = await runtime.poll_events(0)
        for event in trail:
            print(f"[{event.kind.value} t{event.turn}] {event.payload}")
        attach_acp_segment(store, events.session_id, runtime)
        return {
            "session_id": info.native_session_id,
            "garuda_session_id": events.session_id,
            "turn": turn,
            "events": len(trail),
        }
    finally:
        await runtime.close()


__all__ = [
    "RUNTIME_API_VERSION",
    "acp_adapter_for_workspace",
    "attach_acp_segment",
    "cmd_handoff_confirm",
    "cmd_handoff_preview",
    "cmd_inspect",
    "cmd_inspect_registry",
    "cmd_list",
    "cmd_list_registry",
    "cmd_recover",
    "cmd_resume",
    "cmd_support_bundle",
    "configured_registry",
    "load_configured_manifest_dicts",
    "recover_dict",
    "run_acp_task",
    "support_bundle_dict",
]
