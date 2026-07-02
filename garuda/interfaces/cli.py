import asyncio
import json
from pathlib import Path

from garuda.core.sessions import SessionStore
from garuda.interfaces.runner import cleanup_workspace, resolve_environment
from garuda.interfaces.session import AgentSession
from garuda.plugins.hooks import build_hook_registry
from garuda.types import AgentResult


async def stdin_approval(action: str) -> bool:
    print(f"\n[garuda] Approve {action}? [y/N]: ", end="", flush=True)
    answer = await asyncio.to_thread(input)
    return answer.strip().lower() in ("y", "yes")


def _emit_new_events(events, offset: int) -> int:
    all_events = events.get_all()
    for event in all_events[offset:]:
        print(json.dumps(event, default=str))
    return len(all_events)


async def chat_loop(args) -> int:
    agents_dir = Path(args.agents_dir) if args.agents_dir else None
    from garuda.agents.loader import load_profile

    profile = load_profile(args.agent, extra_dir=agents_dir)
    approval = stdin_approval if profile.permission_mode == "smart" else None

    session = await AgentSession.create(
        agent_name=args.agent,
        model=args.model,
        workspace=args.workspace,
        agents_dir=agents_dir,
        mcp_config_path=getattr(args, "mcp_config", None),
        mode=getattr(args, "mode", "standard"),
        approval_handler=approval,
        workspace_kind=getattr(args, "workspace_kind", "local"),
        docker_image=getattr(args, "docker_image", "ubuntu:22.04"),
        docker_host=getattr(args, "docker_host", None),
    )

    # One workspace/environment for the whole chat session, reused across turns.
    env, env_handle = await resolve_environment(
        session.config.workspace_kind,
        args.workspace,
        session.config.docker_image,
        docker_host=session.config.docker_host,
    )

    store = SessionStore()
    events_path = store.begin(
        session_id=session.events.session_id,
        task="(interactive chat)",
        model=args.model,
        agent=session.profile.name,
        workspace=args.workspace,
    )
    session.events.attach_persistence(events_path)
    hooks = build_hook_registry(args.workspace)

    print(
        f"Garuda chat — agent={session.profile.name} model={args.model} "
        f"workspace={session.config.workspace_kind}"
    )
    print("Enter a task (empty line to quit).\n")

    await hooks.on_session_start(task="(interactive chat)", session_id=session.events.session_id)
    last_result: AgentResult | None = None
    events_emitted = 0
    try:
        while True:
            print("task> ", end="", flush=True)
            try:
                task = await asyncio.to_thread(input)
            except EOFError:
                print()
                break
            if not task.strip():
                break

            context = session.prepare_context(task.strip())
            result = await session.agent.run(
                task=task.strip(),
                model=session.model,
                env=env,
                tools=session.tools,
                config=session.config,
                events=session.events,
                permissions=session.permissions,
                hooks=hooks,
                agents_dir=session.agents_dir,
                context=context,
            )
            last_result = result
            if args.json:
                events_emitted = _emit_new_events(session.events, events_emitted)
            print(f"\n{result.final_message}\n")
    except KeyboardInterrupt:
        print()
    finally:
        await cleanup_workspace(env_handle)
        await session.close()
        _persist_chat_session(store, session, last_result)
        await hooks.on_session_end(
            {
                "session_id": session.events.session_id,
                "success": last_result.success if last_result else True,
                "turns": last_result.turns if last_result else 0,
            }
        )
    print("Bye.")
    return 0


def _persist_chat_session(
    store: SessionStore,
    session: AgentSession,
    last_result: AgentResult | None,
) -> None:
    """Save the chat conversation so it can be listed and resumed later."""
    if last_result is not None:
        result = last_result
        if session.context is not None:
            # The shared context holds every turn, not just the final run's view.
            result = AgentResult(
                success=last_result.success,
                final_message=last_result.final_message,
                messages=session.context.get_messages(),
                turns=last_result.turns,
                metadata=last_result.metadata,
            )
    else:
        messages = session.context.get_messages() if session.context else []
        result = AgentResult(success=True, final_message="", messages=messages, turns=0)
    try:
        store.finish(session.events.session_id, result)
    except OSError:
        pass
