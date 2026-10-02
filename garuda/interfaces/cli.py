import asyncio
import contextlib
import functools
import json
import sys

from garuda.core.sessions import SessionStore
from garuda.interfaces.runner import resolve_environment
from garuda.interfaces.session import AgentSession
from garuda.interfaces.tui import ChatRenderer
from garuda.model.factory import safe_model_identity
from garuda.plugins.hooks import build_hook_registry
from garuda.types import AgentResult
from garuda.workspace import evidence


def resolved_model_name(session: AgentSession) -> str:
    """The event-safe resolved reasoning identity every entry point logs."""
    return safe_model_identity(session.model)


async def stdin_approval(action: str, stream=None) -> bool:
    # The prompt is human-facing, never data: in JSONL mode the caller passes
    # stderr so stdout stays parseable.
    print(
        f"\n[garuda] Approve {action}? [y/N]: ",
        end="",
        flush=True,
        file=stream or sys.stdout,
    )
    answer = await asyncio.to_thread(input)
    return answer.strip().lower() in ("y", "yes")


def _drain_events(
    renderer: ChatRenderer,
    events,
    offset: int,
    *,
    render: bool,
    emit_json: bool,
) -> int:
    """Push events appended since ``offset`` to the renderer and/or JSONL stdout.

    Called repeatedly while a turn runs so tool calls and results surface live.
    Returns the new offset.
    """
    all_events = events.get_all()
    for event in all_events[offset:]:
        if emit_json:
            print(json.dumps(event, default=str))
        if render:
            _render_event(renderer, event)
    return len(all_events)


def _render_event(renderer: ChatRenderer, event: dict) -> None:
    etype = event.get("type")
    payload = event.get("payload") or {}
    if etype == "model_response":
        content = payload.get("content")
        if content:
            renderer.on_assistant_delta(content)
    elif etype == "tool_call":
        name = payload.get("name", "")
        args = payload.get("arguments") or {}
        if name == "todo":
            renderer.on_todo(args.get("todos") or [])
        else:
            renderer.on_tool_call(name, args)
    elif etype == "tool_result":
        name = payload.get("name", "")
        if name == "todo":
            return  # already shown as a todo panel from the tool_call
        renderer.on_tool_result(
            name, payload.get("content", ""), bool(payload.get("is_error"))
        )


async def chat_loop(args) -> int:
    from garuda.config.agent_home import resolve_agents_dirs

    agents_dir = resolve_agents_dirs(args.workspace, args.agents_dir)
    from garuda.agents.loader import load_profile

    profile = load_profile(args.agent, extra_dir=agents_dir)
    # In JSONL mode stdout carries one JSON event per line and nothing else, so
    # every human-facing write — prompts, header, status, final message, "Bye." —
    # goes to stderr instead. A consumer piping stdout to a parser must never see
    # decoration.
    human = sys.stderr if args.json else sys.stdout
    approval = (
        functools.partial(stdin_approval, stream=human)
        if profile.permission_mode == "smart"
        else None
    )

    session = await AgentSession.create(
        agent_name=args.agent,
        model=getattr(args, "model", None),
        reasoning_model=getattr(args, "reasoning_model", None),
        collection_model=getattr(args, "collection_model", None),
        no_collection=getattr(args, "no_collection", False),
        workspace=args.workspace,
        agents_dir=agents_dir,
        mcp_config_path=getattr(args, "mcp_config", None),
        # None, not a literal mode: a fallback value here would override the
        # profile's own `mode` for any caller whose args lack the attribute.
        mode=getattr(args, "mode", None),
        permission_mode=getattr(args, "permission_mode", None),
        approval_handler=approval,
        workspace_kind=getattr(args, "workspace_kind", "local"),
        docker_image=getattr(args, "docker_image", "ubuntu:22.04"),
        docker_host=getattr(args, "docker_host", None),
    )

    store = SessionStore()
    # One session lifecycle with `garuda run`, the dashboard and the SDK (B.6):
    # capacity and the workspace lease, then the session record, the baseline,
    # the approval broker and the environment — or a refusal with nothing held.
    from garuda.interfaces import session_service
    from garuda.interfaces.run_guard import lease_mode_for

    recorded: dict[str, str] = {}

    def begin(workspace: str) -> None:
        recorded["events_path"] = store.begin(
            session_id=session.events.session_id,
            task="(interactive chat)",
            model=resolved_model_name(session),
            agent=session.profile.name,
            workspace=workspace,
        )

    try:
        live = await session_service.open_session(
            store,
            session.events.session_id,
            args.workspace,
            workspace_kind=session.config.workspace_kind,
            permissions=getattr(session, "permissions", None),
            lease_mode=lease_mode_for(getattr(session.config, "permission_mode", None)),
            docker_image=getattr(session.config, "docker_image", "ubuntu:22.04"),
            docker_host=getattr(session.config, "docker_host", None),
            begin=begin,
            resolve_env=resolve_environment,
        )
    except session_service.SessionRefused as exc:
        await session.close()
        print(f"Error: chat refused to start: {exc}", file=human)
        return 1
    env = live.env
    events_path = recorded["events_path"]
    session.events.attach_persistence(events_path)
    hooks = build_hook_registry(live.workspace)

    # JSONL mode must keep stdout machine-readable, so rich rendering is off there.
    render = not args.json
    renderer = ChatRenderer(use_rich=render, stream=human)
    renderer.header(
        model=resolved_model_name(session),
        agent=session.profile.name,
        workspace=session.config.workspace_kind,
        session_id=session.events.session_id,
    )

    await hooks.on_session_start(task="(interactive chat)", session_id=session.events.session_id)
    last_result: AgentResult | None = None
    offset = len(session.events.get_all())
    try:
        while True:
            print("task> ", end="", flush=True, file=human)
            try:
                task = await asyncio.to_thread(input)
            except EOFError:
                print(file=human)
                break
            if not task.strip():
                break

            context = session.prepare_context(task.strip())
            run_task = asyncio.create_task(
                live.turn(session.agent.run(
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
                    workspace_delta_loader=live.delta_loader,
                    collection_model=getattr(session, "collection", None),
                    collection_policy=getattr(session, "collection_policy", None),
                ))
            )
            # Run the turn in the background and drain events as they arrive so
            # tool calls/results render live under a "thinking" spinner.
            status = renderer.thinking() if render else contextlib.nullcontext()
            try:
                with status:
                    while not run_task.done():
                        offset = _drain_events(
                            renderer, session.events, offset, render=render, emit_json=args.json
                        )
                        await asyncio.sleep(0.05)
                result = await run_task
            except BaseException as exc:
                run_task.cancel()
                with contextlib.suppress(BaseException):
                    await run_task
                # A failed turn is not a failed session. Ctrl-C and cancellation
                # still propagate (the user meant those); an ordinary error — a
                # provider 500, a bad tool argument — is reported and the prompt
                # comes back, because losing the whole conversation to one bad turn
                # is a worse outcome than the error itself.
                if isinstance(exc, Exception):
                    offset = _drain_events(
                        renderer, session.events, offset, render=render, emit_json=args.json
                    )
                    renderer.on_error(f"{type(exc).__name__}: {exc}")
                    continue
                raise
            offset = _drain_events(
                renderer, session.events, offset, render=render, emit_json=args.json
            )
            last_result = result
            renderer.on_done(result.final_message)
    except KeyboardInterrupt:
        print(file=human)
    finally:
        await session.close()

        async def finish() -> None:
            persisted = await _persist_chat_session(store, session, last_result, live.workspace)
            await hooks.on_session_end(
                {
                    "session_id": session.events.session_id,
                    "success": persisted.success,
                    "turns": persisted.turns,
                }
            )

        # Reaps background processes and tears the environment down, saves the
        # session, then releases the lease last (or quarantines).
        await live.close(finish)
    print("Bye.", file=human)
    return 0


async def _persist_chat_session(
    store: SessionStore,
    session: AgentSession,
    last_result: AgentResult | None,
    workspace: str,
) -> AgentResult:
    """Save the chat conversation so it can be listed and resumed later.

    The final workspace delta is persisted first, from the baseline recorded
    when the chat opened; if it cannot be, the session is saved as failed.
    """
    if last_result is not None:
        result = last_result
        if session.context is not None:
            # The shared context holds every turn, not just the final run's view.
            result = AgentResult(
                success=last_result.success,
                final_message=last_result.final_message,
                messages=session.context.get_messages(),
                turns=last_result.turns,
                metadata=dict(last_result.metadata),
            )
    else:
        messages = session.context.get_messages() if session.context else []
        result = AgentResult(success=True, final_message="", messages=messages, turns=0)
    try:
        result.metadata["workspace_delta"] = await asyncio.to_thread(
            evidence.finish_session_evidence, store, session.events.session_id, workspace
        )
    except Exception as exc:
        result.success = False
        result.final_message = (
            "Chat failed: authoritative workspace delta could not be recorded "
            f"({type(exc).__name__})."
        )
        result.metadata["workspace_delta_error"] = type(exc).__name__
    try:
        store.finish(session.events.session_id, result)
    except OSError:
        pass
    return result
