"""Shared foreground role-run setup and execution used by CLI and starters.

Legacy CLI rendering helpers remain in main; the setup and dispatch sequence
has one owner here, including runtime/role selection and native/ACP routing.
"""

from __future__ import annotations

import os
from pathlib import Path

from garuda.core.events import EventStore


async def run_task(args) -> int:
    import sys

    from garuda.interfaces.main import (
        _accept_session,
        _apply_role,
        _enable_consults,
        _finish_no_edits,
        _no_edits_requested,
        _plan_resume,
        _print_consult_line,
        _print_worktree_note,
        _require_in_place,
        _session_record,
        _session_tags,
        agent_selection,
        check_garuda_config,
        run_acp_command,
        run_agent_task,
    )

    task = args.task
    if args.file:
        task = Path(args.file).read_text(encoding="utf-8")
    if not task:
        print("Error: provide -t/--task or -f/--file", file=sys.stderr)
        return 1
    # garuda.yaml is validated — a conflict or invalid file refuses — before
    # tags, runtimes, models or the workspace are touched (C.1).
    resolved_config = check_garuda_config(args)
    args._config = resolved_config
    if getattr(args, "no_edits", False):
        _require_in_place(args)
        args.permission_mode = "readonly"
    # Session tags are resolved — or refused — before any runtime, model,
    # workspace or prompt exists (B.7).
    attached = None if getattr(args, "starter_record", None) is not None else _session_tags(args, task)
    args._attached = attached

    # Resolve policy before constructing a model, toolkit, workspace, or
    # provider adapter. The selected id is then handed to the matching
    # executor below; the native default is not an implicit bypass.
    from garuda.agents.setup import (
        prepare_runtime_catalog,
        select_initial_runtime_async,
        select_runtime,
    )
    from garuda.eval.costs import estimate_cost
    from garuda.interfaces.runtime_cli import NativeStartupFallback

    # P2's explicit opt-in router may choose a policy target first. P1 then
    # resolves and explains the initial owner from the same trusted registry;
    # its selected id is the executor below, never merely an audit record.
    runtime_catalog = prepare_runtime_catalog(args.workspace)
    args._role_plan = _apply_role(args, resolved_config, runtime_catalog)
    # How a resumed session continues (B.7) is decided before selection: a
    # session of an ACP runtime continues on it — by the agent's own reload
    # when that is proven for the adapter, otherwise through a brief — and
    # `--as` continues on another runtime through a brief.
    resume_plan = _plan_resume(args, runtime_catalog)
    if resume_plan is not None and resume_plan.mode != "native":
        args.runtime = resume_plan.runtime_id
        args.resume = None
        if resume_plan.mode == "brief":
            from garuda.context.tags import with_resume_brief
            from garuda.core.sessions import SessionStore

            attached = with_resume_brief(
                SessionStore(), resume_plan, os.path.realpath(args.workspace), attached
            )
            args._attached = attached
    args._resume_plan = resume_plan
    # `None` means the flag was omitted. Naming a runtime, including `native`,
    # is an explicit choice that routing rules and the classifier never override.
    runtime_named = args.runtime is not None
    requested_runtime = args.runtime if runtime_named else "native"
    if requested_runtime != "native":
        # Resolve aliases and the global disabled gate before selection turns
        # an explicit request into a candidate id. This preserves the public
        # fail-closed error for a disabled alias and avoids treating aliases as
        # unconfigured runtime ids in the generic selector.
        requested_runtime = runtime_catalog.registry.get(
            requested_runtime
        ).runtime_id
    if runtime_named and requested_runtime == "native":
        routed_runtime = "native"
    else:
        routed_runtime = select_runtime(args.workspace, requested_runtime)
    explicit_native = runtime_named and routed_runtime == "native"
    # This catalog is passed through selection and launch. That prevents a
    # selection probe from observing different trusted registry facts than the
    # executor it chooses. When no explicit, profile, or rule source selects,
    # an enabled trusted classifier may recommend one revalidated candidate
    # (#80); it runs before any executor exists and cannot start a runtime.
    initial_plan = await select_initial_runtime_async(
        workspace=args.workspace,
        task=task,
        catalog=runtime_catalog,
        agent=getattr(args, "agent", "build"),
        mode=getattr(args, "mode", None) or "",
        explicit_runtime=(
            routed_runtime if routed_runtime != "native" or explicit_native else None
        ),
        workspace_kind=getattr(args, "workspace_kind", "local"),
        permission_ceiling=getattr(args, "permission_mode", None) or "smart",
        available_runtime_ids=(
            # An explicit, registry-authorized runtime must select its ACP
            # executor even when discovery has already reported a missing
            # binary. The ACP launch path then emits its established loud
            # installation error; it must never silently become native.
            frozenset({routed_runtime}) if routed_runtime != "native" else None
        ),
        cost_estimator=estimate_cost,
    )
    args.runtime = initial_plan.selection.selected
    args._initial_selection = initial_plan.selection
    args._initial_plan = initial_plan
    fallback_store = getattr(args, "_store", None)
    fallback_events = None
    if getattr(args, "runtime", "native") != "native":
        # Resolved before constructing any model, tools, or workspace state,
        # preserving the fail-closed disabled/alias policy on `garuda run`.
        # An ACP selection launches through the ACP path with the same
        # catalog; an alias of the native loop continues below. A refused
        # selection raises `RegistryError`, which `main()` reports as a
        # message with exit status 2 (`_run_with_runtime_gate`).
        from garuda.runtime.protocol import RuntimeKind

        selected = runtime_catalog.registry.get(args.runtime)
        if selected.kind is RuntimeKind.ACP:
            try:
                return await run_acp_command(args, task, runtime_catalog)
            except NativeStartupFallback as fallback:
                # A failed ACP start may transfer exactly once to native only
                # after `run_acp_task` verified the workspace is unchanged.
                # Reuse its session trail so the explanation and both runtime
                # tenures remain one recoverable session.
                args.runtime = fallback.selection.selected
                args._initial_selection = fallback.selection
                fallback_store = fallback.store
                fallback_events = fallback.events
                # ACP may already have selected a worktree for this session.
                # Continue there without creating another branch/worktree.
                prior = fallback_store.load_meta(fallback_events.session_id)
                args.workspace = prior.get("workspace") or args.workspace
                if prior.get("isolation") == "worktree":
                    args.isolation = "shared"

    # Native selection still goes through the common trusted boundary before
    # any toolkit or workspace startup. ACP selection is resolved by
    # ``run_acp_command`` through the same trusted registry service.
    from garuda.agents.setup import prepare_agent_run

    runtime_catalog.select_for_native_facade(args.runtime)
    from garuda.config.agent_home import resolve_agents_dirs
    from garuda.model.config import ConfigError
    from garuda.model.factory import safe_model_identity

    agents_dir = resolve_agents_dirs(args.workspace, args.agents_dir)
    try:
        prepared = await prepare_agent_run(
            agent_selection(args),
            workspace=args.workspace,
            agents_dir=args.agents_dir,
            mcp_config_path=args.mcp_config,
            mode=args.mode,
            permission_mode=args.permission_mode,
            model=getattr(args, "model", None),
            reasoning_model=getattr(args, "reasoning_model", None),
            collection_model=getattr(args, "collection_model", None),
            no_collection=getattr(args, "no_collection", False),
            reasoning_effort=getattr(args, "reasoning_effort", None),
            thinking_budget_tokens=getattr(args, "thinking_budget", None),
            load_project_tools=getattr(args, "load_project_tools", None),
        )
    except (ConfigError, FileNotFoundError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    config = prepared.config
    # Preset first, explicit flags after: every `if args.x` below is a narrower
    # statement of intent than the posture and must win over it.
    if args.max_turns is not None:
        config.max_turns = args.max_turns
    if getattr(args, "deadline_sec", None) is not None:
        config.deadline_sec = args.deadline_sec
    if args.no_verifier:
        config.enable_verifier = False
    if args.no_three_step_summary:
        config.enable_three_step_summary = False
    if getattr(args, "persistent_shell", False):
        config.persistent_shell = True
    if getattr(args, "no_post_edit_diagnostics", False):
        config.post_edit_diagnostics = False
    if getattr(args, "no_post_edit_lint", False):
        config.post_edit_lint = False
    if getattr(args, "no_bootstrap", False):
        config.bootstrap_environment = False
    _enable_consults(args, config)
    config.workspace_kind = args.workspace_kind
    config.docker_image = args.docker_image
    config.docker_host = args.docker_host
    config.sandbox_allow_network = getattr(args, "allow_network", False)
    config.sandbox_require = not getattr(args, "allow_unsandboxed", False)
    config.docker_network = "none" if getattr(args, "no_network", False) else "bridge"
    config.docker_memory = getattr(args, "docker_memory", "2g")
    config.docker_cpus = getattr(args, "docker_cpus", "2")
    from garuda.agents.compile import narrow_docker

    narrow_docker(config, prepared.profile)  # a definition can only narrow these (H.12a)

    model = prepared.reasoning
    permissions = prepared.permissions
    agent = prepared.agent
    tools = prepared.tools
    mcp_manager = prepared.mcp_manager
    if not args.json:
        print(
            f"[garuda] reasoning={safe_model_identity(model)} "
            f"({prepared.provenance['reasoning'].provenance.value})",
        )
        if prepared.collection is not None:
            print(
                f"[garuda] collection={safe_model_identity(prepared.collection)} "
                f"({prepared.provenance['collection'].provenance.value})",
            )
    events = fallback_events or EventStore(session_id=getattr(args, "_session_id", None))
    from garuda.workspace.no_edits import NoEditsGuard

    guard = NoEditsGuard(args.workspace) if _no_edits_requested(args) else None

    result = await run_agent_task(
        task=task,
        model=model,
        agent=agent,
        tools=tools,
        config=config,
        permissions=permissions,
        workspace=args.workspace,
        events=events,
        emit_json=args.json,
        workspace_kind=args.workspace_kind,
        docker_image=args.docker_image,
        docker_host=args.docker_host,
        mcp_manager=mcp_manager,
        agents_dir=agents_dir,
        resume=args.resume,
        resume_all_projects=getattr(args, "all_projects", False),
        session_name=getattr(args, "name", None),
        isolation=getattr(args, "isolation", "shared"),
        context_attached=getattr(args, "_attached", None),
        session_record=_session_record(args),
        runtime_catalog=runtime_catalog,
        runtime_ref=args.runtime,
        initial_selection=args._initial_selection,
        store=fallback_store,
        collection_model=prepared.collection,
        collection_policy=prepared.collection_policy,
    )

    args._completed_session_id = events.session_id
    if args.trajectory:
        events.save(args.trajectory)
    if guard is not None and not _finish_no_edits(args, guard, events.session_id).unchanged:
        return 3  # outputs withheld; the changes stay for you to inspect
    _accept_session(args, events.session_id)
    if not args.json:
        print(result.final_message)
        if getattr(args, "isolation", "shared") != "shared" or args.resume:
            _print_worktree_note(fallback_store, events.session_id)
        _print_consult_line(fallback_store, events.session_id)
    return 0 if result.success else 1
