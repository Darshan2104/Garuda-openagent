"""CLI runtime selection, handoff, resume, and recovery commands (P1.6, issue #37).

`garuda runtime list|inspect` renders discovery; `garuda run --runtime`
selects the executor (native default, unchanged); `garuda runtime handoff`
previews by default and executes the handoff transaction only with
`--confirm`; `garuda runtime resume` continues a persisted session through the
real run lifecycle (which classifies first); `garuda runtime recover`
classifies and recovers sessions. Text by default, JSON with `--json`, and
no switch ever happens without explicit confirmation.

Every path resolves through one trusted catalog
(`garuda.agents.setup.build_runtime_catalog`). ACP launches bind the exact
executable one discovery accepted, hold the workspace lease, persist a
unified session with the runtime segment and child record, record the
workspace baseline before the first prompt, and park approvals in the P0.17
broker (`garuda.interfaces.run_guard`).

A confirmed handoff is one-shot: pause → checkpoint → capture → start target
→ acknowledge (ownership and the target segment persisted together, child
recorded) → close the source → deliver the package as the target's first
prompt → close the target and record `target_state: closed`. The session then
stays owned by the target runtime: `recover` reports it `external`, and native
resume refuses instead of silently taking ownership back.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any

from garuda.acp.catalog import (
    adapter_for_discovered,
    adapter_for_manifest,
    discover,
    discover_for_launch,
    health_of,
)
from garuda.runtime.recovery import recover, report_to_dict
from garuda.runtime.registry import RuntimeRegistry

logger = logging.getLogger(__name__)

RUNTIME_API_VERSION = "1"


#: Upper bound on the target's first (package-delivery) turn in a CLI handoff.
#: A hung target must not keep the command, its lease, and the process alive.
HANDOFF_DELIVERY_TIMEOUT_SEC = 600.0


class NativeStartupFallback(RuntimeError):
    """Signal that an ACP pre-start failure can safely continue natively."""

    def __init__(self, *, store, events, selection):
        super().__init__("ACP startup fallback selected the native executor")
        self.store = store
        self.events = events
        self.selection = selection


def configured_catalog(
    workspace: str = ".",
    *,
    disabled=None,
    global_settings: dict | None = None,
    project_settings: dict | None = None,
):
    """The one trusted runtime catalog for CLI discovery and execution.

    A thin input adapter over `build_runtime_catalog`: by default it reads the
    strict global trust anchor and the workspace's project settings exactly
    like `prepare_runtime_catalog`. Explicit mappings are the test seam.
    """
    from garuda.agents.setup import build_runtime_catalog, prepare_runtime_catalog

    if global_settings is None and project_settings is None and disabled is None:
        return prepare_runtime_catalog(workspace)
    if global_settings is None:
        from garuda.acp.catalog import load_trusted_runtime_settings

        global_settings = load_trusted_runtime_settings()
    if project_settings is None:
        from garuda.config.agent_home import resolve_agent_home

        project_settings = getattr(resolve_agent_home(workspace), "settings", None) or {}
    return build_runtime_catalog(
        global_settings=global_settings,
        project_settings=project_settings,
        disabled=disabled,
    )


def configured_registry(
    workspace: str = ".",
    *,
    disabled=None,
    global_settings: dict | None = None,
    project_settings: dict | None = None,
) -> RuntimeRegistry:
    """The registry of `configured_catalog` (same builder, same gates)."""
    return configured_catalog(
        workspace,
        disabled=disabled,
        global_settings=global_settings,
        project_settings=project_settings,
    ).registry


def load_configured_manifest_dicts(global_settings: dict | None = None) -> list[dict]:
    """Compatibility list for callers that need the trusted global manifests."""
    from garuda.acp.catalog import builtin_manifest_dicts, load_trusted_runtime_settings

    settings = global_settings if global_settings is not None else load_trusted_runtime_settings()
    extras = settings.get("runtimes", [])
    if not isinstance(extras, list):
        raise ValueError("global settings 'runtimes' must be a list of manifests")
    return [
        {
            "runtime_id": "native",
            "kind": "native",
            "version": "builtin",
            "description": "The in-process Garuda loop.",
        },
        *builtin_manifest_dicts(),
        *extras,
    ]


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
    """Resolve one configured ACP target before constructing its adapter."""
    from garuda.acp.catalog import AcpUnavailableError
    from garuda.runtime import RegistryError
    from garuda.runtime.protocol import RuntimeKind

    workspace = os.path.abspath(workspace)
    catalog = configured_catalog(
        workspace,
        disabled=disabled,
        global_settings=global_settings,
        project_settings=project_settings,
    )
    try:
        if argv_override is None:
            manifest, discovered = acp_launch_target(catalog, runtime_name)
            adapter = adapter_for_discovered(
                manifest,
                discovered,
                policy=policy,
                cwd=workspace,
                store=store,
                persist_dir=persist_dir,
            )
        else:
            resolved = catalog.registry.get(runtime_name)
            if resolved.kind is not RuntimeKind.ACP:
                raise ValueError(f"Cannot use runtime {runtime_name!r}: not an ACP runtime")
            manifest = catalog.registry.manifest_for(runtime_name)
            adapter = adapter_for_manifest(
                manifest,
                argv_override=list(argv_override),
                policy=policy,
                cwd=workspace,
                store=store,
                persist_dir=persist_dir,
            )
    except RegistryError as exc:
        if "disabled by user configuration" in str(exc):
            raise
        raise ValueError(f"Cannot use runtime {runtime_name!r} ({exc})") from exc
    except AcpUnavailableError as exc:
        raise RegistryError(f"runtime {runtime_name!r} is not launchable: {exc}") from exc
    return catalog.registry, adapter


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


def acp_launch_target(catalog, runtime_ref: str):
    """Resolve and discover one ACP runtime for launch: `(manifest, record)`.

    Unknown, disabled, native, and not-installed targets refuse here, before
    any session, lease, or process exists. The returned record's absolute
    executable is what `adapter_for_discovered` launches — no second lookup.
    """
    from garuda.acp.catalog import AcpUnavailableError
    from garuda.runtime.protocol import RuntimeKind

    resolved = catalog.registry.get(runtime_ref)
    if resolved.kind is not RuntimeKind.ACP:
        raise AcpUnavailableError(
            resolved.runtime_id, "not an ACP runtime; use `--runtime native`"
        )
    return discover_for_launch(catalog.registry, runtime_ref)


def cmd_list(
    manifests, *, as_json: bool = False, disabled=None, project_disabled=frozenset()
) -> str:
    found = discover(
        manifests, disabled=disabled or frozenset(), project_disabled=project_disabled
    )
    if as_json:
        return json.dumps([health_of(d) for d in found], indent=2)
    lines = []
    for entry in found:
        lines.extend(entry.describe())
    return "\n".join(lines) + "\n"


def cmd_list_registry(
    registry: RuntimeRegistry, *, as_json: bool = False, project_disabled=frozenset()
) -> str:
    return cmd_list(
        registry.manifests,
        as_json=as_json,
        disabled=registry.disabled_ids,
        project_disabled=project_disabled,
    )


def cmd_inspect(
    manifests,
    runtime_id: str,
    *,
    as_json: bool = False,
    disabled=None,
    project_disabled=frozenset(),
) -> str:
    found = {
        d.runtime_id: d
        for d in discover(
            manifests, disabled=disabled or frozenset(), project_disabled=project_disabled
        )
    }
    if runtime_id not in found:
        raise KeyError(f"unknown runtime {runtime_id!r}")
    entry = found[runtime_id]
    if as_json:
        return json.dumps(health_of(entry), indent=2)
    return "\n".join([*entry.describe(), *entry.describe_auth()]) + "\n"


def cmd_inspect_registry(
    registry: RuntimeRegistry,
    runtime_id: str,
    *,
    as_json: bool = False,
    project_disabled=frozenset(),
) -> str:
    return cmd_inspect(
        registry.manifests,
        runtime_id,
        as_json=as_json,
        disabled=registry.disabled_ids,
        project_disabled=project_disabled,
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
    workspace: str | None = None,
    pack_manager=None,
    state=None,
    catalog=None,
    target_argv_override: list[str] | None = None,
    approval=None,
    delivery_timeout: float = HANDOFF_DELIVERY_TIMEOUT_SEC,
) -> str:
    """Execute the handoff transaction against real runtimes (one-shot).

    Resolves and discovers the target through the trusted catalog first
    (unknown, disabled, or missing targets refuse before anything moves), then
    resumes the source — which runs recovery and refuses a session another
    runtime already owns — and takes the workspace lease. The transaction
    acknowledges before delivery: ownership and the target segment are
    persisted, the source closes, and only then is the package sent as the
    target's first prompt (bounded by `delivery_timeout`, approvals via the
    broker; `approval=None` denies every ask, audited). A delivery failure is
    a target failure, never a rollback over the target's changes. The target
    is closed when the command ends and `target_state: closed` is recorded.

    ``catalog`` and ``target_argv_override`` are test seams: the override
    replaces the discovered argv for the protocol fixture only.
    """
    from garuda.context.pack import compile_handoff
    from garuda.context.state_card import WorkingState
    from garuda.interfaces.run_guard import WorkspaceLeaseGuard, broker_approval_handler
    from garuda.runtime.handoff import execute_handoff, record_target_outcome
    from garuda.runtime.native import NativeGarudaRuntime
    from garuda.runtime.protocol import RuntimeKind

    if target_id == "native":
        return (
            "handoff refused: the target is the native runtime itself — "
            "resume the session instead (`garuda runtime resume`).\n"
        )
    if workspace is None:
        recorded = store.load_meta(session_id).get("workspace")
        # A relative record (native sessions store ".") names whatever
        # directory the command happens to run in; guessing would lease,
        # diff, and hand over the wrong tree.
        if not isinstance(recorded, str) or not os.path.isabs(recorded):
            raise ValueError(
                f"session {session_id} records no absolute workspace; "
                "pass --workspace explicitly"
            )
        workspace = recorded
    workspace = os.path.abspath(workspace)
    if catalog is None:
        catalog = configured_catalog(workspace)
    resolved = catalog.registry.get(target_id)  # unknown/disabled fail closed here
    if resolved.kind is not RuntimeKind.ACP:
        return (
            f"handoff refused: {resolved.runtime_id!r} is not an external runtime — "
            "resume the session instead (`garuda runtime resume`).\n"
        )
    launch = None
    if target_argv_override is None:
        # One discovery, checked now, launched later: the factory below binds
        # this record's absolute executable, so a PATH change after the check
        # cannot substitute a different binary.
        launch = acp_launch_target(catalog, target_id)
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
    # Classifies first: prepared switches roll back, ambiguous trails refuse,
    # and a session an external runtime owns is never taken back natively.
    await source.resume(native_session_id=session_id)
    lease = WorkspaceLeaseGuard(workspace, session_id, capacity_key=resolved.runtime_id)
    lease.acquire()
    try:
        lease.start_heartbeat()
        handler, _broker = broker_approval_handler(
            store, session_id, resolved.runtime_id, handler=approval
        )

        def _target_factory():
            # Store-less at start: the target binds its segment and child
            # record once the acknowledgement makes it the active owner.
            if launch is None:
                return adapter_for_manifest(
                    resolved,
                    argv_override=target_argv_override,
                    cwd=workspace,
                    approval_handler=handler,
                    persist_dir=str(store.session_dir(session_id)),
                )
            manifest, record = launch
            return adapter_for_discovered(
                manifest,
                record,
                cwd=workspace,
                approval_handler=handler,
                persist_dir=str(store.session_dir(session_id)),
            )

        delivered: dict[str, Any] = {}

        async def _deliver(target) -> None:
            if not handoff_body:
                raise RuntimeError("handoff package was not generated")
            delivered["turn"] = await lease.race(
                target.prompt(handoff_body[0], timeout=delivery_timeout)
            )
            events, _ = await target.poll_events(0)
            delivered["reply"] = "".join(
                str(event.payload.get("chunk", ""))
                for event in events
                if event.turn == delivered["turn"] and "chunk" in event.payload
            )

        tx, target = await execute_handoff(
            session_id=session_id,
            source=source,
            target_factory=_target_factory,
            store=store,
            checkpoint=_checkpoint,
            deliver=_deliver,
            workspace=workspace,
        )
        # One-shot command: the target is closed (child reaped and retired)
        # and the session records how its owner ended.
        close_error: Exception | None = None
        try:
            await target.close()
        except Exception as exc:
            close_error = exc
        record_target_outcome(
            store,
            session_id,
            tx,
            "closed" if close_error is None else "failed",
            delivered_turn=delivered.get("turn"),
            **({"reason": f"close: {type(close_error).__name__}"} if close_error else {}),
        )
        if close_error is not None:
            raise close_error
    finally:
        await lease.release()
    reply = " ".join(delivered.get("reply", "").split())
    if len(reply) > 500:
        reply = reply[:240] + " ... " + reply[-240:]
    return (
        f"handoff acknowledged: {session_id} -> {resolved.runtime_id} "
        f"(phase={tx.phase.value}; package delivered as target turn "
        f"{delivered.get('turn')}; target closed)\n"
        f"  target reply: {reply or '(none)'}\n"
        f"  ownership stays with {resolved.runtime_id}; native resume of this "
        "session is refused (see `garuda runtime recover`)\n"
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


def cmd_reclaim(store, session_id: str, *, leases=None) -> str:
    """Return a handed-off session to its native tenure (see `reclaim_native`)."""
    from garuda.runtime.recovery import reclaim_native

    report = reclaim_native(store, session_id, leases=leases)
    return (
        f"session {report.session_id}: ownership reclaimed by native "
        f"({report.state.value}); resume with `garuda runtime resume --session "
        f"{report.session_id}`\n"
    )


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


async def _resume_or_brief(runtime, make_runtime, plan, store, session_id, task, workspace,
                           attached):
    """Reload the agent's own session (a proven adapter), or — when the agent
    no longer offers it — start fresh with the source session's brief (B.7)."""
    from garuda.acp.adapter import ResumeUnavailable
    from garuda.context.tags import with_resume_brief
    from garuda.runtime.resume import as_brief

    try:
        info = await runtime.resume(native_session_id=plan.native_session_id,
                                    session_id=session_id)
        return info, runtime, attached
    except ResumeUnavailable as drift:
        try:
            await runtime.close()
        except Exception:
            logger.warning("Closing the refused resume failed", exc_info=True)
        fallback = as_brief(plan, f"{plan.runtime_id}: {drift}")
        store.update_meta(session_id, fallback.record())
        attached = with_resume_brief(store, fallback, workspace, attached)
        runtime = make_runtime()
        return await runtime.start(task=task, session_id=session_id), runtime, attached


async def run_acp_task(
    task: str,
    *,
    runtime_id: str,
    workspace: str = ".",
    store=None,
    catalog=None,
    approval=None,
    prompt_timeout: float | None = None,
    emit=print,
    initial_selection=None,
    initial_plan=None,
    attached=None,
    name: str | None = None,
    resume_plan=None,
    role_plan=None,
    lease_capability=None,
    consult_host=None,
) -> dict[str, Any]:
    """Run one task on an ACP runtime under the same invariants as native.

    Order: resolve + discover (refuse before anything exists) → workspace
    lease (a live foreign mutating lease refuses) → persisted unified session
    whose only segment is this runtime → workspace baseline → broker-backed
    approvals → start (the adapter fills the segment and records the child)
    → prompt raced against the lease heartbeat → close (child reaped and
    retired) → final workspace delta → lease release, on every path including
    cancellation. The session status is `completed` when the agent ended its
    turn — Garuda did not verify the result — or `failed`.
    """
    from garuda.core.events import EventStore
    from garuda.core.sessions import SessionStore
    from garuda.interfaces.run_guard import WorkspaceLeaseGuard, broker_approval_handler
    from garuda.runtime.session import RuntimeSegment
    from garuda.workspace.evidence import begin_session_evidence, finish_session_evidence

    workspace = os.path.abspath(workspace)
    if catalog is None:
        catalog = configured_catalog(workspace)
    # A read-only ACP role runs only in proven Docker confinement (C.8a); its
    # adapter runs in the image, so no host executable is required or used.
    confinement = None
    if role_plan is not None and role_plan.kind == "acp" and role_plan.permissions == "readonly":
        from garuda.acp.catalog import DiscoveredRuntime
        from garuda.workspace.confined_acp import Confinement, preflight

        confinement = Confinement.from_config(role_plan.harness)
        preflight(workspace, confinement)
        resolved_rt = catalog.registry.get(runtime_id)
        manifest = next(m for m in catalog.registry.manifests
                        if m.runtime_id == resolved_rt.runtime_id)
        record = DiscoveredRuntime(runtime_id=manifest.runtime_id, kind="acp", available=True)
    else:
        manifest, record = acp_launch_target(catalog, runtime_id)
    # A role's exact model and effort must be settable on this adapter
    # identity — decided before anything is launched (C.3).
    role_options: dict[str, str] = {}
    if role_plan is not None:
        from garuda.runtime.roles import acp_options

        role_options = acp_options(role_plan, getattr(record, "version", "unknown"))

    store = store or SessionStore()
    events = EventStore()
    session_id = events.session_id
    if lease_capability is not None:
        from garuda.interfaces.session_service import acquire_lease

        lease = acquire_lease(workspace, session_id, capacity_key=manifest.runtime_id,
                              capability=lease_capability)
    else:
        # A read-only role shares the workspace with other readers.
        readonly = role_plan is not None and role_plan.permissions == "readonly"
        lease = WorkspaceLeaseGuard(workspace, session_id, capacity_key=manifest.runtime_id,
                                    mode="read-only" if readonly else "mutating")
        lease.acquire()
    runtime = None
    began = False
    transferred_to_native = False
    outcome: dict[str, Any] | None = None
    try:
        store.begin(
            session_id,
            task=task,
            model=f"acp:{manifest.runtime_id}",
            agent=manifest.runtime_id,
            workspace=workspace,
            name=name,
            runtime_segment=RuntimeSegment(
                runtime_id=manifest.runtime_id, kind="acp", version=manifest.version
            ),
        )
        began = True
        if confinement is not None:
            store.update_meta(session_id, {"confinement": {
                "kind": "docker", "image": confinement.image, "source": "read-only"}})
        if resume_plan is not None:
            store.update_meta(session_id, resume_plan.record())
        if attached is not None:
            from garuda.context.tags import record_links

            record_links(store, session_id, attached)
        if initial_selection is not None:
            if initial_selection.selected != manifest.runtime_id:
                raise ValueError(
                    "initial selection and ACP launch disagree: "
                    f"{initial_selection.selected!r} != {manifest.runtime_id!r}"
                )
            from garuda.runtime.selection import record_initial_selection

            record_initial_selection(store, session_id, initial_selection)
        from garuda.agents.setup import resolve_and_record_routing

        resolve_and_record_routing(
            workspace=workspace,
            store=store,
            session_id=session_id,
            pin=runtime_id,
        )
        lease.start_heartbeat()
        handler, _broker = broker_approval_handler(
            store, session_id, manifest.runtime_id, handler=approval
        )
        def make_runtime():
            runtime = build_runtime()
            if consult_host is not None and confinement is None:  # no host socket in a container
                runtime.attach_consult(consult_host)  # exposed only for a proved adapter (G.3)
            return runtime

        def build_runtime():
            if confinement is not None:
                from garuda.acp.catalog import adapter_for_manifest
                from garuda.workspace.confined_acp import CONTAINER_WORKSPACE, docker_argv

                command = list(confinement.command or manifest.command)
                return adapter_for_manifest(
                    manifest, argv_override=docker_argv(workspace, confinement, command),
                    cwd=CONTAINER_WORKSPACE, store=store, approval_handler=handler,
                    persist_dir=str(store.session_dir(session_id)),
                )
            return adapter_for_discovered(
                manifest,
                record,
                cwd=workspace,
                store=store,
                approval_handler=handler,
                persist_dir=str(store.session_dir(session_id)),
            )

        runtime = make_runtime()
        # Capture a baseline before start solely for the narrow startup
        # fallback gate. Evidence begins only after a successful start, so a
        # native fallback owns its own evidence span on this same session.
        from garuda.workspace.diff import capture_baseline

        baseline_before = capture_baseline(workspace)
        try:
            if resume_plan is not None and resume_plan.mode == "acp":
                info, runtime, attached = await _resume_or_brief(
                    runtime, make_runtime, resume_plan, store, session_id, task,
                    workspace, attached,
                )
            else:
                info = await runtime.start(task=task, session_id=session_id)
        except Exception as start_error:
            if initial_plan is None or resume_plan is not None:
                raise
            from garuda.runtime.selection import (
                SelectionError,
                record_initial_selection,
                select_startup_fallback,
            )
            from garuda.runtime.session import RuntimeSegment

            fallback = select_startup_fallback(
                initial_plan.selection,
                initial_plan.request,
                initial_plan.candidates,
                baseline_before=baseline_before,
                workspace=workspace,
            )
            if fallback.selected != "native":
                raise SelectionError(
                    "ACP startup fallback must select the native executor"
                ) from start_error
            # Preserve the failed ACP tenure and transfer ownership to native
            # in the same session before the native runtime is constructed.
            record_initial_selection(store, session_id, fallback)
            store.attach_runtime_segment(
                session_id, RuntimeSegment(runtime_id="native", kind="native")
            )
            transferred_to_native = True
            raise NativeStartupFallback(
                store=store, events=events, selection=fallback
            ) from start_error
        if role_plan is not None:
            applied = await runtime.set_config_options(role_options)
            store.update_meta(session_id, {"role": role_plan.record(
                adapter={"runtime_id": manifest.runtime_id,
                         "version": getattr(record, "version", "unknown")},
                options=applied)})
            from garuda.observability import usage as usage_ledger

            usage_ledger.record_fallback_start(store, session_id)
        begin_session_evidence(store, session_id, workspace, "local")
        from garuda.context.tags import prompt_with

        turn = await lease.race(
            runtime.prompt(prompt_with(attached, task), timeout=prompt_timeout)
        )
        events, _ = await runtime.poll_events(0)
        for event in events:
            emit(f"[{event.kind.value} t{event.turn}] {event.payload}")
        # The agent's own words, bounded: what a brief of this session reports
        # as its output (B.7). Never a transcript.
        reply = "".join(
            str(e.payload.get("chunk") or "") for e in events
            if e.kind.value == "message" and "thought" not in e.payload
        )
        if reply:
            store.update_meta(session_id, {"final_message": reply[-2000:]})
        outcome = {
            "session_id": session_id,
            "native_session_id": info.native_session_id,
            "turn": turn,
            "events": len(events),
        }
    except asyncio.CancelledError:
        if began:
            try:
                from garuda.runtime.recovery import record_cancel

                record_cancel(store, session_id, boundary="turn", reason="task cancelled")
            except Exception:
                logger.warning("Failed to record ACP task cancellation", exc_info=True)
        raise
    finally:
        try:
            if runtime is not None:
                try:
                    await runtime.close()
                except Exception:
                    logger.warning("ACP runtime close failed", exc_info=True)
            if began and not transferred_to_native:
                status = "failed"
                if outcome is not None:
                    try:
                        outcome["workspace_delta"] = await asyncio.to_thread(
                            finish_session_evidence, store, session_id, workspace
                        )
                        status = "completed"
                    except Exception as exc:
                        outcome["workspace_delta_error"] = type(exc).__name__
                try:
                    from garuda.runtime.session_state import finished

                    store.update_meta(
                        session_id,
                        {
                            "status": status,
                            "verified": False,
                            # The external harness's own checks are not Garuda's
                            # completion gate: no self-check, no verification.
                            "state": finished(success=status == "completed"),
                        },
                    )
                except Exception:
                    logger.warning("Failed to record ACP session status", exc_info=True)
                if outcome is not None:
                    outcome["status"] = status
        finally:
            await lease.release()
    return outcome


__all__ = [
    "HANDOFF_DELIVERY_TIMEOUT_SEC",
    "acp_launch_target",
    "RUNTIME_API_VERSION",
    "NativeStartupFallback",
    "acp_adapter_for_workspace",
    "attach_acp_segment",
    "cmd_handoff_confirm",
    "cmd_handoff_preview",
    "cmd_inspect",
    "cmd_inspect_registry",
    "cmd_list",
    "cmd_list_registry",
    "cmd_reclaim",
    "cmd_recover",
    "cmd_resume",
    "cmd_support_bundle",
    "configured_catalog",
    "configured_registry",
    "load_configured_manifest_dicts",
    "recover_dict",
    "run_acp_task",
    "support_bundle_dict",
]
